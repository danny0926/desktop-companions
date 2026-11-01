"""Focused regression coverage for provider discovery after desktop login."""

import copy
from collections import deque
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from desktop_pet.agent import service as service_module
from desktop_pet.agent.providers import ProviderCancelled
from desktop_pet.contracts import AgentRequest, ProactiveRequest
from desktop_pet.app import PetApplication


_STATUS = {"provider_mode": "codex_subscription", "llm_ready": False, "configured": True,
           "model": "gpt-6-luna", "reason": "Not discovered", "probes": []}
_REPLY = json.dumps({"speech_text_ja": "こんにちは。", "subtitle_zh": "你好。",
                     "attitude": "daily", "intensity": "light", "delivery": "natural"})


class _Provider:
    mode, model = "codex_subscription", "gpt-6-luna"

    def __init__(self):
        self.closed = False

    def complete_cancellable(self, messages, schema, **kwargs):
        return _REPLY

    def close(self):
        self.closed = True


class LoginRecoveryTests(unittest.TestCase):
    def _service(self, root, mode="codex_subscription"):
        config = json.loads(Path(service_module._DEFAULT_CONFIG).read_text(encoding="utf-8-sig"))
        config["provider"]["mode"] = mode
        config["provider"]["model"] = "gpt-6-luna"
        config_path = Path(root) / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        return config_path

    def test_missing_subscription_is_rediscovered_for_manual_and_proactive_requests(self):
        for proactive in (False, True):
            with self.subTest(proactive=proactive), tempfile.TemporaryDirectory() as root:
                config_path = self._service(root)
                provider = _Provider()
                with patch.object(service_module, "select_provider", side_effect=[(None, copy.deepcopy(_STATUS)),
                                                                                  (provider, copy.deepcopy(_STATUS))]) as select:
                    service = service_module.AgentService(config_path, root)
                    try:
                        if proactive:
                            request = ProactiveRequest(
                                turn_id="proactive-recovery", event_id="proactive-recovery",
                                kind="companion_topic", observed_at=time.monotonic(),
                                category="other", observation_source="injected",
                            )
                            service.generate_proactive_reply(request)
                        else:
                            service.generate_reply(AgentRequest("manual-recovery", "話そう"))
                        self.assertEqual(select.call_count, 2)
                        self.assertIs(service._provider, provider)
                    finally:
                        service.close()

    def test_offline_demo_does_not_retry_provider_discovery(self):
        with tempfile.TemporaryDirectory() as root:
            config_path = self._service(root, "offline_demo")
            with patch.object(service_module, "select_provider", wraps=service_module.select_provider) as select:
                service = service_module.AgentService(config_path, root)
                try:
                    reply = service.generate_reply(AgentRequest("offline", "話そう"))
                    self.assertEqual(reply.provider_mode, "offline_demo")
                    self.assertEqual(select.call_count, 1)
                finally:
                    service.close()

    def test_cancel_or_close_during_discovery_closes_candidate_without_publishing(self):
        for close_service in (False, True):
            with self.subTest(close_service=close_service), tempfile.TemporaryDirectory() as root:
                config_path = self._service(root)
                entered, release = threading.Event(), threading.Event()
                candidate = _Provider()
                discovery_count = 0
                def discover(_config):
                    nonlocal discovery_count
                    discovery_count += 1
                    if discovery_count == 1:
                        return None, copy.deepcopy(_STATUS)
                    entered.set()
                    release.wait(3)
                    return candidate, copy.deepcopy(_STATUS)
                with patch.object(service_module, "select_provider", side_effect=discover):
                    service = service_module.AgentService(config_path, root)
                    errors = []
                    worker = threading.Thread(target=lambda: self._capture_error(
                        errors, lambda: service.generate_reply(AgentRequest("cancel-discovery", "話そう"))))
                    worker.start()
                    self.assertTrue(entered.wait(2))
                    if close_service:
                        service.close()
                    else:
                        service.cancel("cancel-discovery")
                    release.set()
                    worker.join(3)
                    self.assertFalse(worker.is_alive())
                    self.assertTrue(errors and isinstance(errors[0], ProviderCancelled))
                    self.assertTrue(candidate.closed)
                    self.assertIsNone(service._provider)
                    service.close()

    def test_discard_resets_status_only_for_matching_proactive_turn(self):
        class Status:
            value = "二乃看看目前畫面…"
            def set(self, value):
                self.value = value

        class Player:
            item = {"turn_id": "manual-newer"}
            stopped = False
            def stop(self):
                self.stopped = True

        def app_for(turn_id):
            app = SimpleNamespace(
                turn_id=turn_id, _turn_is_proactive=True, _audio_queue=deque([
                    {"turn_id": "discarded"}, {"turn_id": "manual-newer"},
                ]), _synthesis_pending=True, _heard=[], _reply=None,
                _turn_complete=False, _active_proactive_decision=object(),
                status=Status(), player=Player(),
            )
            app._log = lambda *args, **kwargs: None
            app.controller = SimpleNamespace(record_delivery=lambda *args: None)
            app._hide_bubble = lambda: None
            app._set_animation = lambda value: None
            return app

        matched = app_for("discarded")
        PetApplication._handle_event(matched, {"type": "proactive_discarded", "turn_id": "discarded"})
        self.assertEqual(matched.status.value, "二乃在這裡，想聊什麼都可以。")
        self.assertFalse(matched.player.stopped, "unrelated playback must continue")
        self.assertEqual([item["turn_id"] for item in matched._audio_queue], ["manual-newer"])

        unmatched = app_for("manual-newer")
        PetApplication._handle_event(unmatched, {"type": "proactive_discarded", "turn_id": "discarded"})
        self.assertEqual(unmatched.status.value, "二乃看看目前畫面…")
        self.assertEqual(unmatched.turn_id, "manual-newer")

    @staticmethod
    def _capture_error(errors, action):
        try:
            action()
        except BaseException as exc:
            errors.append(exc)


if __name__ == "__main__":
    unittest.main()
