"""Voice-library and local-playback contracts; no GUI or inference runs."""
import hashlib
import json
from pathlib import Path
import queue
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest

from desktop_pet.contracts import AgentReply
from desktop_pet.shell.controller import ConversationController
from desktop_pet.shell.replay import ReplayStore
from desktop_pet.shell.test_interaction import fake_app


def reply(turn="agent-a", text="同じ台詞。"):
    return {"turn_id": turn, "speech_text_ja": text, "subtitle_zh": "同一句話。",
            "attitude": "gentle", "intensity": "light", "delivery": "soft", "caption_ja": "優しく話す。",
            "expression": "smile", "motion": "idle", "provider_mode": "codex_subscription"}


def wav_result(directory, name="a", *, segment=0, text="同じ台詞。", value=17):
    import wave
    path = directory / f"{name}.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        stream.writeframes(value.to_bytes(2, "little") * 480)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"status": "generated", "playable": True, "wav_path": str(path), "request_id": name,
            "segment_id": str(segment), "text_spoken_ja": text, "model_sha256": "model-hash",
            "adapter_sha256": "adapter-hash", "audio": {"sha256": digest, "seconds": 0.01}}


class ReplayContracts(unittest.TestCase):
    def test_event_backfill_preserves_occurrences_and_cancelled_metadata(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = wav_result(root, "first")
            second = wav_result(root, "second", value=33)
            retry = wav_result(root, "retry", value=71)
            # Mutable request metadata is deliberately cancelled; immutable
            # generated events and matching original bytes remain authoritative.
            (root / "first.json").write_text('{"status":"cancelled"}', encoding="utf-8")
            events = [{"event": "reply", "turn_id": "op-a", "reply": reply("agent-a")},
                      {"event": "voice_result", "turn_id": "op-a", "result": first},
                      {"event": "reply", "turn_id": "op-b", "reply": reply("agent-b")},
                      {"event": "voice_result", "turn_id": "op-b", "result": second},
                      {"event": "voice_retry_started", "turn_id": "retry-a", "agent_turn_id": "agent-a"},
                      {"event": "voice_result", "turn_id": "retry-a", "result": retry}]
            log = root / "events.jsonl"
            log.write_text("\n".join(json.dumps(event, ensure_ascii=False) for event in events), encoding="utf-8")
            store = ReplayStore(root / "library", workspace=root)
            store.import_events([log])
            records = store.records()
            self.assertEqual(len(records), 3)
            self.assertTrue(all(record["complete"] for record in records))
            self.assertEqual({record["operation_id"] for record in records}, {"op-a", "op-b", "retry-a"})
            a, b = store.for_reply("agent-a"), store.for_reply("agent-b")
            self.assertNotEqual(a["id"], b["id"])
            self.assertEqual(store.playlist(b["id"])[0]["wav_path"], second["wav_path"])
            self.assertEqual(b["pieces"][0]["model_sha256"], "model-hash")

    def test_segment_order_partial_missing_and_hash_mismatch_are_explicit(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ReplayStore(root / "library", workspace=root)
            record_id = store.add_reply(reply(text="一つ。二つ。"), "op")
            second = wav_result(root, "second", segment=1, text="二つ。")
            first = wav_result(root, "first", text="一つ。")
            store.add_result("op", second)
            self.assertFalse(store.get(record_id)["complete"])
            store.add_result("op", first)
            self.assertTrue(store.get(record_id)["complete"])
            self.assertEqual([item["segment_id"] for item in store.playlist(record_id)], ["0", "1"])
            Path(first["wav_path"]).unlink()
            partial = store.get(record_id)
            self.assertFalse(partial["complete"])
            self.assertTrue(partial["generated_complete"])
            self.assertEqual(partial["available_pieces"], 1)
            Path(second["wav_path"]).write_bytes(b"changed")
            with self.assertRaises(FileNotFoundError):
                store.playlist(record_id)

    def test_favorite_copies_exact_bytes_survives_source_loss_and_recent_bound(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ReplayStore(root / "library", workspace=root, recent_limit=2)
            record_id = store.add_reply(reply(), "op", created_at=1)
            result = wav_result(root)
            original = Path(result["wav_path"]).read_bytes()
            store.add_result("op", result)
            saved = store.set_favorite(record_id)
            favorite_path = Path(saved["pieces"][0]["favorite_wav_path"])
            self.assertEqual(favorite_path.read_bytes(), original)
            Path(result["wav_path"]).unlink()
            for index in range(4):
                store.add_reply(reply(f"new-{index}"), f"new-{index}", created_at=10 + index)
            restored = ReplayStore(root / "library", workspace=root, recent_limit=2)
            self.assertTrue(restored.get(record_id)["complete"])
            self.assertEqual(len(restored.records()), 3)
            self.assertEqual(len(restored.records(favorites_only=True)), 1)
            self.assertEqual(Path(restored.playlist(record_id)[0]["wav_path"]).read_bytes(), original)
            restored.set_favorite(record_id, False)
            self.assertTrue(favorite_path.exists())

    def test_replay_uses_unique_local_operations_no_services_or_memory_and_input_preempts(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ReplayStore(root / "library", workspace=root)
            rid = store.add_reply(reply(), "original")
            store.add_result("original", wav_result(root))
            events = queue.Queue()
            deliveries, generations, voice_calls = [], [], []
            agent = SimpleNamespace(
                record_delivery=lambda *args, **kwargs: deliveries.append((args, kwargs)),
                generate_reply=lambda request: (generations.append(request) or AgentReply.from_dict(reply(request.turn_id))),
                cancel=lambda **kwargs: generations.append(("cancel", kwargs)))
            voice = SimpleNamespace(cancel=lambda **kwargs: voice_calls.append(kwargs))
            controller = ConversationController({"conversation_id": "test"}, events.put, agent=agent, voice=voice)
            app = fake_app()
            app.controller, app.replays = controller, store
            app.events = events
            app._last_reply = reply("last-genuine")
            original_memory = dict(app._last_reply)
            try:
                self.assertTrue(app.replay_record(rid))
                first = app.turn_id
                batch = []
                while not batch or batch[-1]["type"] != "finished":
                    batch.append(events.get(timeout=1))
                self.assertEqual([event["type"] for event in batch], ["audio", "finished"])
                for event in batch:
                    app._handle_event(event)
                self.assertEqual(len(app._audio_queue), 1)
                self.assertEqual(generations, [])
                self.assertEqual(voice_calls, [])
                self.assertEqual(app.health, {})
                controller.record_delivery(first, "heard again")
                self.assertEqual(deliveries, [])
                self.assertEqual(app._last_reply, original_memory)
                app._input_activity()
                self.assertFalse(controller.current(first))
                self.assertEqual(len(app._audio_queue), 0)
                self.assertEqual(deliveries, [])
                self.assertTrue(app.replay_record(rid))
                self.assertNotEqual(app.turn_id, first)
                second = app.turn_id
                app.send_text("新しい入力")
                self.assertFalse(controller.current(second))
                self.assertNotEqual(app.turn_id, second)
                self.assertEqual(len(app._audio_queue), 0)
            finally:
                controller.stop()

    def test_replay_does_not_wait_for_cancelled_generation_worker(self):
        entered, release = threading.Event(), threading.Event()
        generations = []
        def generate(request):
            generations.append(request.turn_id)
            entered.set()
            release.wait(2)
            return AgentReply.from_dict(reply(request.turn_id))
        events = queue.Queue()
        controller = ConversationController({"conversation_id": "test"}, events.put,
            agent=SimpleNamespace(generate_reply=generate, cancel=lambda **kwargs: None))
        try:
            controller.start("held generation", speak=False)
            self.assertTrue(entered.wait(1))
            operation = controller.replay_audio({"agent_turn_id": "original"}, [
                {"status": "replay", "wav_path": "owned-original.wav", "text_spoken_ja": "原音。"}])
            emitted = []
            while not emitted or emitted[-1]["type"] != "finished":
                event = events.get(timeout=1)
                if event.get("turn_id") == operation:
                    emitted.append(event)
            self.assertFalse(release.is_set())
            self.assertEqual([event["type"] for event in emitted], ["audio", "finished"])
            self.assertEqual(len(generations), 1)
        finally:
            controller.stop()
            release.set()

    def test_transcript_controls_bind_duplicate_text_to_turn_not_text(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ReplayStore(root / "library", workspace=root)
            ids = []
            for turn in ("one", "two"):
                ids.append(store.add_reply(reply(turn), turn))
                store.add_result(turn, wav_result(root, turn))
            bindings, clicked = {}, []
            text = SimpleNamespace(winfo_exists=lambda: True, configure=lambda **kwargs: None,
                                   delete=lambda *args: None, tag_names=lambda: (), tag_delete=lambda tag: None,
                                   tag_configure=lambda *args, **kwargs: None, insert=lambda *args: None,
                                   tag_bind=lambda tag, event, callback: bindings.update({(tag, event): callback}),
                                   see=lambda *args: None)
            app = fake_app()
            app.replays, app.transcript = store, text
            app.replay_record = clicked.append
            app._transcript_parts = [("reply", "二乃", "同じ台詞。", "one"),
                                     ("reply", "二乃", "同じ台詞。", "two"),
                                     ("reply", "二乃", "旧形式も表示する。")]
            app._render_transcript()
            bindings[("replay_0", "<Button-1>")](None)
            bindings[("replay_1", "<Button-1>")](None)
            self.assertEqual(clicked, ids)


if __name__ == "__main__":
    unittest.main()
