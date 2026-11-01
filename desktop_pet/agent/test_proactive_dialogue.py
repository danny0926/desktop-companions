"""Proactive prompt bounds and complete-sentence delivery, without model calls."""

import copy
from contextlib import closing
import json
import re
import tempfile
import time
import unittest

from desktop_pet.agent.performance import ReplyFormatError
from desktop_pet.agent.service import AgentService
from desktop_pet.contracts import AgentRequest, ProactiveRequest, ScreenObservation
from desktop_pet.observation.screen import _png_from_bgra


def _reply(text):
    return json.dumps({
        "speech_text_ja": text, "subtitle_zh": "一起聊聊吧。",
        "attitude": "playful", "intensity": "light", "delivery": "teasing",
    }, ensure_ascii=False)


class _Provider:
    mode, model, base_url = "codex_subscription", "gpt-6-luna", ""
    supports_screen_images = True

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete_cancellable(self, messages, schema, **options):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(schema), options))
        return self.responses.pop(0)


def _event(name, *, kind="companion_topic", maximum=36):
    return ProactiveRequest(
        turn_id=name, event_id=name, kind=kind, observed_at=time.monotonic(),
        category="other", observation_source="injected", max_reply_chars=maximum,
        opener_topic="soft_visible_comment" if kind == "screen_observation" else "companion_aside",
    )


def _image():
    return ScreenObservation(
        png_bytes=_png_from_bgra(bytes((0, 0, 0, 255)) * 16 * 16, 16, 16),
        image_id="synthetic-test-scene", observed_at=time.monotonic(), source="injected",
        scope="active_window", active_window_token="synthetic", width=16, height=16,
    )


class ProactiveDialogueTests(unittest.TestCase):
    def test_image_prompt_uses_the_request_bound_and_keeps_observation_out_of_user_history(self):
        provider = _Provider(_reply("次の仕草、私にも選ばせて。"))
        with tempfile.TemporaryDirectory() as root, closing(AgentService(data_dir=root, provider=provider)) as service:
            event = _event("bounded-image", kind="screen_observation", maximum=24)
            image = _image()
            reply = service.generate_proactive_reply(event, screenshot=image)
            messages, schema, options = provider.calls[0]
            persona_schema = json.loads(messages[0]["content"].rsplit("\n", 1)[1])
            observation = json.loads(messages[-1]["content"])
            self.assertEqual(persona_schema["properties"]["speech_text_ja"]["maxLength"], 24)
            self.assertEqual(schema["properties"]["speech_text_ja"]["maxLength"], 24)
            self.assertRegex(reply.speech_text_ja, schema["properties"]["speech_text_ja"]["pattern"])
            self.assertEqual(observation["event"]["kind"], "screen_observation")
            self.assertEqual(observation["event"]["observation_source"], "injected")
            self.assertTrue(all(message["role"] == "system" for message in messages))
            self.assertIs(options["screenshot"], image)
            # The image-specific instruction must choose a conversational intent;
            # it must not ask the renderer to enumerate visible content first.
            instruction = observation["instruction_ja"]
            self.assertIn("相手に直接話しかける", instruction)
            self.assertIn("画面の要約、状況報告、UIや実装の解説を返さない", instruction)
            self.assertEqual(service.memory.recent_messages(event.conversation_id), [])
            self.assertEqual(reply.attitude, "playful")
            self.assertNotIn(reply.caption_ja, reply.speech_text_ja)

    def test_incomplete_or_overlong_proactive_reply_is_rewritten_without_truncation(self):
        incomplete = "次の仕草、あなたと一緒に考え"
        overlong = "ねえ、" + "次の仕草も一緒に選んでほしいの。" * 3
        corrected = "ねえ、次の仕草は私にも選ばせて。"
        for invalid in (incomplete, overlong):
            provider = _Provider(_reply(invalid), _reply(corrected))
            with self.subTest(invalid_length=len(invalid)), tempfile.TemporaryDirectory() as root, \
                    closing(AgentService(data_dir=root, provider=provider)) as service:
                event = _event("repair")
                reply = service.generate_proactive_reply(event)
                self.assertEqual(reply.speech_text_ja, corrected)
                self.assertEqual(len(provider.calls), 2)
                repair_messages, repair_schema, options = provider.calls[-1]
                self.assertEqual(repair_messages[-1]["role"], "system")
                self.assertEqual(json.loads(repair_messages[-2]["content"])["speech_text_ja"], invalid)
                self.assertFalse(any(message["role"] == "user" for message in repair_messages))
                self.assertNotIn("screenshot", options)
                self.assertIsNone(re.search(repair_schema["properties"]["speech_text_ja"]["pattern"], incomplete))
                self.assertEqual(service.memory.recent_messages(event.conversation_id), [])
                service.record_delivery(event.turn_id, conversation_id=event.conversation_id,
                                        heard_text=reply.speech_text_ja)
                history = service.memory.recent_messages(event.conversation_id)
                self.assertEqual(history[-1], {"role": "assistant", "content": corrected})
                self.assertNotIn(invalid, json.dumps(history, ensure_ascii=False))

    def test_incomplete_screen_reply_fails_clearly_without_resending_image_or_saving_a_reply(self):
        provider = _Provider(_reply("次の仕草、あなたと一緒に考え"))
        with tempfile.TemporaryDirectory() as root, closing(AgentService(data_dir=root, provider=provider)) as service:
            event = _event("incomplete-image", kind="screen_observation")
            with self.assertRaisesRegex(ReplyFormatError, "主動搭話未能生成完整短句，這次沒有播放"):
                service.generate_proactive_reply(event, screenshot=_image())
            self.assertEqual(len(provider.calls), 1)
            row = service.memory.get_proactive_turn(event.conversation_id, event.turn_id)
            self.assertEqual(row["delivery_status"], "failed")
            self.assertIsNone(row["reply_json"])
            self.assertEqual(service.memory.recent_messages(event.conversation_id), [])
            self.assertIn("沒有播放", service.health()["last_error"])

    def test_normal_user_reply_keeps_its_existing_length_and_punctuation_contract(self):
        provider = _Provider(_reply("うん、あなたの話を聞いているわ"))
        with tempfile.TemporaryDirectory() as root, closing(AgentService(data_dir=root, provider=provider)) as service:
            reply = service.generate_reply(AgentRequest("normal-user", "少し話そう"))
            messages, schema, options = provider.calls[0]
            self.assertEqual(reply.speech_text_ja, "うん、あなたの話を聞いているわ")
            self.assertEqual(schema["properties"]["speech_text_ja"]["maxLength"], 260)
            self.assertNotIn("pattern", schema["properties"]["speech_text_ja"])
            self.assertEqual(messages[-1]["role"], "user")


if __name__ == "__main__":
    unittest.main()
