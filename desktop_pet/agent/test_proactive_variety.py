"""Regression coverage for actual heard history and bounded varied remarks."""

from contextlib import closing
import json
import tempfile
import unittest

from desktop_pet.agent.memory import MemoryStore
from desktop_pet.agent.performance import build_reply
from desktop_pet.agent.proactive_variety import repetition_reason
from desktop_pet.agent.providers import ProactiveEventRejected, ProviderCancelled
from desktop_pet.agent.service import AgentService
from desktop_pet.contracts import AgentReply
from desktop_pet.agent.test_proactive_dialogue import _Provider, _event, _image, _reply


PANCAKE = "ねえ、今度パンケーキ焼くんだけど、一緒に食べない？"
REPHRASED = "今度はホットケーキを一緒に食べに行きましょ。"
NEW_TOPIC = "服を選ぶとき、色と形ならどっちを先に決める？"


def _store(memory, turn_id, text, *, conversation_id="desktop-default", heard=None):
    event = _event(turn_id)
    memory.reserve_proactive_turn(conversation_id, turn_id, event.event_record())
    reply = build_reply(json.loads(_reply(text)), turn_id=turn_id, provider_mode="codex_subscription")
    memory.save_proactive_reply(conversation_id, turn_id, reply.to_dict())
    if heard is not None:
        memory.record_delivery(conversation_id, turn_id, heard_text=heard, status="interrupted")
    return reply


class ProactiveVarietyTests(unittest.TestCase):
    def test_interrupted_formatted_reply_stores_only_valid_heard_prefix(self):
        with tempfile.TemporaryDirectory() as root, closing(AgentService(data_dir=root, provider=_Provider())) as service:
            event = _event("formatted-prefix")
            service.memory.reserve_proactive_turn(event.conversation_id, event.turn_id, event.event_record())
            full = build_reply(
                {"speech_text_ja": PANCAKE, "display_text_ja": f"**{PANCAKE}**",
                 "subtitle_zh": "一起聊聊吧。"},
                turn_id=event.turn_id, provider_mode="codex_subscription",
            )
            service.memory.save_proactive_reply(event.conversation_id, event.turn_id, full.to_dict())
            self.assertTrue(service.record_delivery(event.turn_id, conversation_id=event.conversation_id,
                                                    heard_text="", status="interrupted"))
            prefix = PANCAKE[:10]
            self.assertTrue(service.record_delivery(event.turn_id, conversation_id=event.conversation_id,
                                                    heard_text=prefix, status="interrupted"))
            stored = service.memory.get_proactive_turn(event.conversation_id, event.turn_id)
            reply = AgentReply.from_dict(json.loads(stored["reply_json"]))
            self.assertIsNone(reply.display_text_ja)
            self.assertEqual(reply.speech_text_ja, prefix)
            self.assertEqual(stored["heard_text"], prefix)

    def test_initial_empty_ack_keeps_pending_payload_then_only_heard_words_enter_context(self):
        with tempfile.TemporaryDirectory() as root, closing(AgentService(data_dir=root, provider=_Provider())) as service:
            _store(service.memory, "ack", PANCAKE)
            self.assertTrue(service.record_delivery("ack", conversation_id="desktop-default", heard_text="", status="interrupted"))
            pending = service.memory.get_proactive_turn("desktop-default", "ack")
            self.assertEqual(pending["delivery_status"], "generated")
            self.assertIsNotNone(pending["reply_json"])
            self.assertEqual(service.memory.recent_messages("desktop-default"), [])
            self.assertEqual(service.memory.recent_proactive_messages("desktop-default"), [])
            prefix = "ねえ、今度パンケーキ焼くんだけど、"
            self.assertTrue(service.record_delivery("ack", conversation_id="desktop-default", heard_text=prefix, status="interrupted"))
            self.assertEqual(service.memory.recent_proactive_messages("desktop-default"), [{"role": "assistant", "content": prefix}])
            self.assertEqual(service.memory.recent_messages("desktop-default")[-1]["content"], prefix)
            self.assertTrue(service.record_delivery("ack", conversation_id="desktop-default", heard_text=PANCAKE))
            self.assertEqual(service.memory.recent_proactive_messages("desktop-default")[-1]["content"], PANCAKE)

    def test_prompt_history_is_heard_only_and_scoped_to_character_and_conversation(self):
        provider = _Provider(_reply(NEW_TOPIC))
        with tempfile.TemporaryDirectory() as root, closing(AgentService(data_dir=root, provider=provider)) as service:
            _store(service.memory, "heard", PANCAKE, heard=PANCAKE)
            _store(service.memory, "pending", "お茶にしない？")
            _store(service.memory, "cancelled", "少し休憩しましょ。")
            service.cancel("cancelled")
            self.assertFalse(service.record_delivery("cancelled", conversation_id="desktop-default", heard_text="少し休憩しましょ。"))
            _store(service.memory, "other-conversation", "二人でお出かけしない？", conversation_id="elsewhere", heard="二人でお出かけしない？")
            other = MemoryStore(service.memory.database, "other-character")
            _store(other, "other-character", "さっきの話の続きを聞かせて。", heard="さっきの話の続きを聞かせて。")
            service.generate_proactive_reply(_event("next"))
            messages = provider.calls[0][0]
            observation = json.loads(messages[-1]["content"])
            self.assertEqual(observation["recent_delivered_proactive_ja"], [PANCAKE])
            self.assertNotIn("ちょうどお茶をいれたところ", messages[0]["content"])
            self.assertIn("親しいデスクトップの仲間", messages[0]["content"])

    def test_semantic_rephrasing_is_rejected_but_a_new_subject_is_accepted(self):
        for earlier, candidate in (
            (PANCAKE, REPHRASED),
            ("紅茶でもいれて、一息つかない？", "こーひーを飲んで少し休みましょ。"),
            ("今度のデート、どこに行きたい？", "次は二人でお出かけしましょ。"),
            ("さっきの話、続きを聞かせて。", "この前の話、もう少し聞きたいわ。"),
        ):
            with self.subTest(candidate=candidate):
                self.assertIsNotNone(repetition_reason(candidate, [earlier]))
                self.assertIsNone(repetition_reason(NEW_TOPIC, [earlier]))
        self.assertIsNotNone(repetition_reason("今夜は何を食べたい？", ["甘いものが欲しくなっちゃった。", "夜食は私にも選ばせて。 "]))

    def test_one_nonvisual_regeneration_changes_subject_without_remembering_rejected_candidate(self):
        provider = _Provider(_reply(REPHRASED), _reply(NEW_TOPIC))
        with tempfile.TemporaryDirectory() as root, closing(AgentService(data_dir=root, provider=provider)) as service:
            _store(service.memory, "past", PANCAKE, heard=PANCAKE)
            event = _event("regenerated")
            reply = service.generate_proactive_reply(event)
            self.assertEqual(reply.speech_text_ja, NEW_TOPIC)
            self.assertEqual(len(provider.calls), 2)
            self.assertIn("同じ内容の言い換えではなく", provider.calls[1][0][-1]["content"])
            self.assertLessEqual(provider.calls[1][2]["timeout_seconds"], provider.calls[0][2]["timeout_seconds"])
            self.assertIs(provider.calls[0][2]["cancel_event"], provider.calls[1][2]["cancel_event"])
            self.assertEqual(service.memory.recent_proactive_messages(event.conversation_id), [{"role": "assistant", "content": PANCAKE}])
            stored = service.memory.get_proactive_turn(event.conversation_id, event.turn_id)
            self.assertNotIn(REPHRASED, stored["reply_json"])
            self.assertEqual(stored["delivery_status"], "generated")

    def test_repeated_repair_is_discarded_and_the_next_normal_opportunity_can_speak(self):
        provider = _Provider(_reply(REPHRASED), _reply("今度はパンケーキを一緒に食べましょ。"), _reply(NEW_TOPIC))
        with tempfile.TemporaryDirectory() as root, closing(AgentService(data_dir=root, provider=provider)) as service:
            _store(service.memory, "past", PANCAKE, heard=PANCAKE)
            event = _event("rejected")
            with self.assertRaisesRegex(ProactiveEventRejected, "這次沒有播放"):
                service.generate_proactive_reply(event)
            self.assertEqual(len(provider.calls), 2)
            row = service.memory.get_proactive_turn(event.conversation_id, event.turn_id)
            self.assertEqual(row["delivery_status"], "discarded")
            self.assertIsNone(row["reply_json"])
            self.assertFalse(service.record_delivery(event.turn_id, conversation_id=event.conversation_id, heard_text=REPHRASED))
            self.assertEqual(service.generate_proactive_reply(_event("later")).speech_text_ja, NEW_TOPIC)
            self.assertTrue(service.health()["llm_ready"])

    def test_cancel_during_regeneration_suppresses_the_late_valid_result(self):
        class CancelRepair(_Provider):
            def complete_cancellable(self, messages, schema, **options):
                result = super().complete_cancellable(messages, schema, **options)
                if len(self.calls) == 2:
                    service.cancel(options["turn_id"])
                return result

        provider = CancelRepair(_reply(REPHRASED), _reply(NEW_TOPIC))
        with tempfile.TemporaryDirectory() as root, closing(AgentService(data_dir=root, provider=provider)) as service:
            _store(service.memory, "past", PANCAKE, heard=PANCAKE)
            event = _event("cancel-repair")
            with self.assertRaises(ProviderCancelled):
                service.generate_proactive_reply(event)
            self.assertEqual(len(provider.calls), 2)
            row = service.memory.get_proactive_turn(event.conversation_id, event.turn_id)
            self.assertEqual(row["delivery_status"], "cancelled")
            self.assertIsNone(row["reply_json"])
            self.assertEqual(service.memory.recent_proactive_messages(event.conversation_id), [{"role": "assistant", "content": PANCAKE}])

    def test_repeated_screen_candidate_is_rejected_without_resending_the_image(self):
        provider = _Provider(_reply(REPHRASED))
        with tempfile.TemporaryDirectory() as root, closing(AgentService(data_dir=root, provider=provider)) as service:
            _store(service.memory, "past", PANCAKE, heard=PANCAKE)
            event, image = _event("screen-repeat", kind="screen_observation"), _image()
            with self.assertRaises(ProactiveEventRejected):
                service.generate_proactive_reply(event, screenshot=image)
            self.assertEqual(len(provider.calls), 1)
            self.assertIs(provider.calls[0][2]["screenshot"], image)
            self.assertIsNone(service.memory.get_proactive_turn(event.conversation_id, event.turn_id)["reply_json"])


if __name__ == "__main__":
    unittest.main()
