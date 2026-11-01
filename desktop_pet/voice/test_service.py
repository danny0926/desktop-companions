"""Control-plane tests; these do not import torch or load a voice model."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from .service import DEFAULT_CONFIG, VoiceService
from .text import normalize_for_speech


class _GateBackend:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.cancelled = []
        self.concurrent = 0
        self.max_concurrent = 0

    def synthesize(self, request, cancelled):
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self.calls.append(request)
        self.started.set()
        self.release.wait(timeout=3)
        self.concurrent -= 1
        if request.get("_op") == "prewarm":
            return {"status": "ready", "audio_generated": False}
        # Deliberately returns even after cancel: service must suppress it.
        return {"status": "generated", "wav_path": "unused-test.wav", "watermark_complete": True}

    def cancel(self, request_id):
        self.cancelled.append(request_id)

    def health(self):
        return {"backend": "control_test_only"}

    def close(self):
        self.release.set()


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pet-voice-control-")
        self.path = Path(self.temp.name) / "voice.json"
        self.config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        self.config["output_dir"] = self.temp.name
        self.config["queue_capacity"] = 1
        self.config["resources"]["admission_wait_seconds"] = 0
        self.path.write_text(json.dumps(self.config), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def request(self, turn="a", segment="0"):
        return {"text_ja": "おはよう。", "caption_ja": "自然な会話。", "turn_id": turn, "segment_id": segment}

    def test_fifo_bounded_queue_and_single_compute(self):
        backend = _GateBackend()
        with VoiceService(self.path, backend=backend) as service:
            first = service.submit(self.request(segment="0"))
            self.assertTrue(backend.started.wait(timeout=1))
            second = service.submit(self.request(segment="1"))
            third = service.submit(self.request(segment="2"))
            self.assertEqual(service.wait(third, 1)["status"], "queue_full")
            backend.release.set()
            self.assertEqual(service.wait(first, 1)["status"], "generated")
            self.assertEqual(service.wait(second, 1)["status"], "generated")
            self.assertEqual([request["segment_id"] for request in backend.calls], ["0", "1"])
            self.assertEqual(backend.max_concurrent, 1)

    def test_new_turn_drops_queued_and_late_audio(self):
        backend = _GateBackend()
        with VoiceService(self.path, backend=backend) as service:
            old = service.submit(self.request())
            self.assertTrue(backend.started.wait(timeout=1))
            queued = service.submit(self.request(segment="1"))
            result = service.begin_turn("b")
            self.assertIn(old, result["discarded_request_ids"])
            self.assertEqual(service.wait(old, 1)["status"], "stale_turn")
            self.assertEqual(service.wait(queued, 1)["status"], "stale_turn")
            current = service.submit(self.request("b"))
            backend.release.set()
            self.assertEqual(service.wait(current, 1)["status"], "generated")
            stale = service.get_request(old)
            self.assertIsNone(stale["wav_path"])
            self.assertFalse(stale["playable"])
            self.assertIn(old, backend.cancelled)
            self.assertEqual([request["turn_id"] for request in backend.calls], ["a", "b"])

    def test_blocked_resources_do_not_launch_a_process(self):
        from . import resources
        with patch.object(resources, "memory_status", return_value={"physical_available_bytes": 1024, "commit_available_bytes": 1024}), \
             patch.object(resources, "gpu_status", return_value={"gpu_free_bytes": 1024}):
            with VoiceService(self.path) as service:
                result = service.synthesize(self.request(), timeout=1)
                self.assertEqual(result["status"], "blocked_memory")
                self.assertIsNone(service.health()["worker_pid"])
                self.assertIsNone(result["wav_path"])

    def test_pronunciation_is_limited_to_complete_valid_dates(self):
        self.assertEqual(normalize_for_speech("二乃、2026-11-01だよ。"), "二乃、にせんにじゅうろくねんじゅういちがつついたちだよ。")
        self.assertEqual(normalize_for_speech("二乃 123 2026-02-30"), "二乃 123 2026-02-30")
        self.assertEqual(normalize_for_speech("2024年2月29日"), "にせんにじゅうよねんにがつにじゅうくにち")

    def test_prewarm_survives_turn_change_and_profile_is_per_request(self):
        backend = _GateBackend()
        with VoiceService(self.path, backend=backend) as service:
            warmup = service.submit_prewarm()
            self.assertTrue(backend.started.wait(timeout=1))
            service.begin_turn("a")
            self.assertEqual(service.get_request(warmup)["status"], "running")
            backend.release.set()
            self.assertEqual(service.wait(warmup, 1)["status"], "ready")
            service.set_profile("interactive_6_sway")
            current = service.synthesize(self.request(), timeout=1)
            baseline = service.synthesize(self.request(segment="1"), timeout=1, profile="quality_40")
            self.assertEqual(current["synthesis_profile"], "interactive_6_sway")
            self.assertEqual(baseline["synthesis_profile"], "quality_40")
            self.assertEqual([call.get("synthesis_profile") for call in backend.calls], [None, "interactive_6_sway", "quality_40"])

    def test_close_preserves_completed_artifact_metadata(self):
        backend = _GateBackend()
        backend.release.set()
        service = VoiceService(self.path, backend=backend)
        result = service.synthesize(self.request(), timeout=1)
        service.close()
        saved = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "generated")
        self.assertEqual(saved["wav_path"], result["wav_path"])

    def test_resource_wait_is_observable_and_cancels_without_compute(self):
        from .resources import ResourceWaitCancelled, wait_for_resources
        cancelled = threading.Event()
        notices = []
        def notice(value):
            notices.append(value)
            cancelled.set()
        with self.assertRaises(ResourceWaitCancelled):
            wait_for_resources(lambda: {"physical_available_bytes": 20}, {"physical_available_bytes": 30},
                               stage="tokenizers", phase="cold_load", timeout_seconds=20,
                               cancelled=cancelled.is_set, on_wait=notice, interval_seconds=0.001)
        self.assertEqual(notices[0]["category"], "host_ram")
        self.assertEqual(notices[0]["shortfall_bytes"], 10)
        self.assertEqual(notices[0]["phase"], "cold_load")

    def test_resource_wait_recovers_instead_of_terminal_failure(self):
        from .resources import wait_for_resources
        snapshots = iter(({"physical_available_bytes": 20}, {"physical_available_bytes": 35}))
        notices = []
        result = wait_for_resources(lambda: next(snapshots), {"physical_available_bytes": 30},
                                    stage="imports", phase="cold_load", timeout_seconds=1,
                                    on_wait=notices.append, interval_seconds=0.001)
        self.assertEqual(result["physical_available_bytes"], 35)
        self.assertIsNone(notices[-1])


if __name__ == "__main__":
    unittest.main()
