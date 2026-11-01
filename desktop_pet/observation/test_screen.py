"""Focused screen privacy, freshness and isolated image transport contracts."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from desktop_pet.agent.codex_subscription import CodexSubscriptionProvider
from desktop_pet.agent.providers import ProactiveEventRejected, ProviderCancelled
from desktop_pet.agent.service import AgentService
from desktop_pet.contracts import ProactiveRequest, ScreenObservation
from desktop_pet.observation.screen import ScreenCapture, _CaptureTarget, _png_from_bgra


_PNG = _png_from_bgra(bytes((64, 120, 240, 255)) * 16 * 16, 16, 16)


def _image(*, source="injected", observed_at=None):
    return ScreenObservation(
        png_bytes=_PNG, image_id="screen-test", observed_at=time.monotonic() if observed_at is None else observed_at,
        source=source, scope="active_window", active_window_token="123:456:abc", width=16, height=16,
    )


class _Backend:
    source = "injected"

    def __init__(self):
        self.target = _CaptureTarget(456, os.getpid() + 100, (0, 0, 3840, 2160), "123:456:abc")
        self.captures = 0
        self.on_capture = None

    def foreground(self, scope):
        return self.target

    def capture(self, target, max_edge):
        self.captures += 1
        self.max_edge = max_edge
        if self.on_capture:
            self.on_capture()
        return _PNG, 16, 16


class ScreenCaptureTests(unittest.TestCase):
    def test_one_bounded_capture_and_metadata_only_serialization(self):
        backend = _Backend()
        capture = ScreenCapture(backend=backend)
        image = capture.capture()
        self.assertIsInstance(image, ScreenObservation)
        self.assertEqual((backend.captures, backend.max_edge), (1, 1440))
        self.assertEqual(image.png_bytes, _PNG)
        self.assertNotIn("png", json.dumps(image.to_dict()))
        self.assertNotIn("PNG", repr(image))
        with self.assertRaises(TypeError):
            asdict(image)

    def test_owned_foreground_and_unavailable_desktop_do_not_capture(self):
        backend = _Backend()
        capture = ScreenCapture(backend=backend)
        backend.target = _CaptureTarget(456, os.getpid(), (0, 0, 800, 600), "owned")
        self.assertIsNone(capture.capture())
        backend.target = None
        self.assertIsNone(capture.capture())
        self.assertEqual(backend.captures, 0)

    def test_changed_cancelled_expired_and_closed_observations_fail_quietly(self):
        now = [100.0]
        backend = _Backend()
        capture = ScreenCapture(backend=backend, clock=lambda: now[0])
        image = capture.capture(expires_at=120)
        self.assertTrue(capture.is_current(image))
        backend.target = _CaptureTarget(456, backend.target.pid, backend.target.bounds, "new-tab-or-bounds")
        self.assertFalse(capture.is_current(image))
        # Metadata checks must not take another screenshot.
        self.assertEqual(backend.captures, 1)
        now[0] = 161
        self.assertFalse(capture.is_current(image))
        self.assertIsNone(capture.capture(expires_at=160))
        cancel = threading.Event()
        backend.on_capture = cancel.set
        self.assertIsNone(capture.capture(cancel_event=cancel))
        capture.close()
        self.assertFalse(capture.is_current(image))
        self.assertIsNone(capture.capture())


class CodexImageTransportTests(unittest.TestCase):
    def test_image_flag_subscription_guards_and_owned_temp_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            recorded = {}

            class Process:
                returncode = 0

                def poll(self):
                    return 0

                def wait(self, timeout):
                    return 0

                def communicate(self, prompt, timeout):
                    self_test.assertEqual(recorded["image_path"].read_bytes(), _PNG)
                    payload = json.loads(prompt)
                    self_test.assertTrue(payload["attached_screen_observation"]["untrusted_observation_data"])
                    self_test.assertNotIn("active_window_token", payload["attached_screen_observation"])
                    return ('{"type":"item.completed","item":{"type":"agent_message","text":"{}"}}\n'
                            '{"type":"turn.completed","usage":{}}'), ""

            def spawn(arguments, **kwargs):
                recorded["arguments"] = arguments
                recorded["image_path"] = Path(arguments[arguments.index("--image") + 1])
                recorded["environment"] = kwargs["env"]
                return Process()

            self_test = self
            with patch("desktop_pet.agent.codex_subscription._command", return_value=["codex.exe"]), \
                    patch("desktop_pet.agent.codex_subscription.subprocess.Popen", side_effect=spawn), \
                    patch.dict(os.environ, {"OPENAI_API_KEY": "test-only", "CODEX_API_KEY": "test-only"}):
                provider = CodexSubscriptionProvider({"model": "gpt-6-luna", "codex": {"temporary_root": root}})
                self.assertEqual(provider.complete([], {}, timeout_seconds=5, screenshot=_image()), "{}")
            arguments = recorded["arguments"]
            self.assertEqual(arguments.count("--image"), 1)
            self.assertEqual(arguments[arguments.index("--model") + 1], "gpt-6-luna")
            for guard in ("--ignore-user-config", "--ephemeral", 'forced_login_method="chatgpt"',
                          "mcp_servers={}", 'web_search="disabled"'):
                self.assertIn(guard, arguments)
            self.assertEqual(arguments[arguments.index("--sandbox") + 1], "read-only")
            self.assertNotIn("OPENAI_API_KEY", recorded["environment"])
            self.assertNotIn("CODEX_API_KEY", recorded["environment"])
            self.assertFalse(recorded["image_path"].exists())
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_cancel_reaps_only_matching_run_and_deletes_image(self):
        with tempfile.TemporaryDirectory() as root:
            started, stopped = threading.Event(), threading.Event()
            recorded, errors = {}, []

            class Process:
                returncode = None

                def poll(self):
                    return self.returncode

                def wait(self, timeout):
                    stopped.wait(timeout)
                    return self.returncode

                def communicate(self, prompt, timeout):
                    started.set()
                    stopped.wait(timeout)
                    return "", ""

            process = Process()

            def spawn(arguments, **kwargs):
                recorded["path"] = Path(arguments[arguments.index("--image") + 1])
                return process

            def stop(owned_process):
                self.assertIs(owned_process, process)
                owned_process.returncode = -1
                stopped.set()

            def complete():
                try:
                    provider.complete_cancellable([], {}, timeout_seconds=4, turn_id="look-1",
                                                  cancel_event=cancel, screenshot=_image())
                except BaseException as error:
                    errors.append(error)

            with patch("desktop_pet.agent.codex_subscription._command", return_value=["codex.exe"]), \
                    patch("desktop_pet.agent.codex_subscription.subprocess.Popen", side_effect=spawn), \
                    patch("desktop_pet.agent.codex_subscription._kill_tree", side_effect=stop):
                provider = CodexSubscriptionProvider({"model": "gpt-6-luna", "codex": {"temporary_root": root}})
                cancel = threading.Event()
                worker = threading.Thread(target=complete, daemon=True)
                worker.start()
                self.assertTrue(started.wait(2))
                self.assertFalse(provider.cancel("different-turn"))
                self.assertTrue(recorded["path"].is_file())
                self.assertTrue(provider.cancel("look-1", cancel_event=cancel))
                worker.join(2)
                provider.close()
            self.assertFalse(worker.is_alive())
            self.assertIsInstance(errors[0], ProviderCancelled)
            self.assertFalse(recorded["path"].exists())
            self.assertEqual(list(Path(root).iterdir()), [])


class ScreenAgentTests(unittest.TestCase):
    def test_image_is_not_a_fake_user_message_or_persistent_payload(self):
        class Provider:
            mode, model, base_url = "codex_subscription", "gpt-6-luna", ""
            supports_screen_images = True

            def complete_cancellable(self, messages, schema, **kwargs):
                self.messages, self.kwargs = messages, kwargs
                return json.dumps({"speech_text_ja": "そのコード、何を作ってるの？", "subtitle_zh": "那段程式在做什麼呀？",
                                   "attitude": "daily", "intensity": "light", "delivery": "natural"}, ensure_ascii=False)

        with tempfile.TemporaryDirectory() as root:
            provider = Provider()
            service = AgentService(data_dir=root, provider=provider)
            image = _image()
            event = ProactiveRequest(turn_id="look", event_id="look", kind="screen_observation",
                                     observed_at=time.monotonic(), category="coding", observation_source="injected",
                                     opener_topic="curious_visible_question")
            reply = service.generate_proactive_reply(event, screenshot=image)
            self.assertIs(provider.kwargs["screenshot"], image)
            self.assertTrue(all(message["role"] == "system" for message in provider.messages))
            instruction = json.loads(provider.messages[-1]["content"])["instruction_ja"]
            for silent_image_limit in ("音声入力はない", "声の良し悪し", "作業の進捗", "二乃や吹き出しは観察の話題にせず"):
                self.assertIn(silent_image_limit, instruction)
            row = service.memory.get_proactive_turn(event.conversation_id, event.turn_id)
            saved = json.loads(row["event_json"])
            self.assertEqual(saved["screen_observation"], image.metadata())
            self.assertNotIn("png_bytes", row["event_json"])
            self.assertEqual(service.memory.recent_messages(event.conversation_id), [])
            service.record_delivery(event.turn_id, conversation_id=event.conversation_id, heard_text=reply.speech_text_ja)
            context = service.memory.recent_messages(event.conversation_id)
            self.assertEqual(context[-1], {"role": "assistant", "content": reply.speech_text_ja})
            self.assertTrue(all(message["role"] != "user" for message in context))
            self.assertNotIn("png_bytes", json.dumps(context))
            service.close()

    def test_missing_expired_or_mismatched_image_is_rejected_before_generation(self):
        class Provider:
            mode, model, base_url = "codex_subscription", "gpt-6-luna", ""
            supports_screen_images = True

            def complete_cancellable(self, *args, **kwargs):
                raise AssertionError("Rejected observations must not invoke a model")

        with tempfile.TemporaryDirectory() as root:
            service = AgentService(data_dir=root, provider=Provider())
            event = ProactiveRequest(turn_id="look", event_id="look", kind="screen_observation",
                                     observed_at=time.monotonic(), category="coding", observation_source="injected")
            for image in (None, _image(observed_at=time.monotonic() - 61), _image(source="win32")):
                with self.subTest(image=image):
                    with self.assertRaises(ProactiveEventRejected):
                        service.generate_proactive_reply(event, screenshot=image)
            service.close()


if __name__ == "__main__":
    unittest.main()
