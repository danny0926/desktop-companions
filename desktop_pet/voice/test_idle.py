"""Injected pressure contracts; no torch, checkpoint or GPU process is loaded."""
from __future__ import annotations

import copy
import unittest
from types import SimpleNamespace

from .idle import IdlePressurePolicy
from .resources import GIB
from .service import load_config


class IdlePressureTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        # Existing cases exercise the immediate-unload branch explicitly.
        self.config["resources"]["admission_wait_seconds"] = 0
        self.config["resources"]["idle_grace_extra_seconds"] = 0
        self.live_model = object()
        self.live_reference = object()
        self.state = {"physical_available_bytes": int(2 * GIB), "commit_available_bytes": int(1.25 * GIB),
                      "gpu_allocated_bytes": 100, "gpu_reserved_bytes": 150}
        self.notifications = []
        self.closed = False

    def policy(self, reclaim):
        return IdlePressurePolicy(self.config, snapshot=lambda: copy.deepcopy(self.state), reclaim=reclaim,
                                  notify=self.notifications.append, closing=lambda: self.closed)

    def test_reclaim_preserves_live_model_reference_and_allocation_for_reuse(self):
        model, reference = self.live_model, self.live_reference
        def reclaim():
            self.state["gpu_reserved_bytes"] = self.state["gpu_allocated_bytes"]
            self.state["commit_available_bytes"] = int(1.75 * GIB)
            return {"live_tensor_data_pointers_unchanged": True}
        result = self.policy(reclaim).check(trigger="post_request_pressure")
        self.assertEqual(result["action"], "keep")
        self.assertIs(self.live_model, model)
        self.assertIs(self.live_reference, reference)
        self.assertEqual(self.state["gpu_allocated_bytes"], 100)
        self.assertEqual(result["cache_reclaim"]["gpu_cache_released_bytes"], 50)
        self.assertEqual(result["cache_reclaim"]["global_commit_change_bytes"], int(.5 * GIB))
        self.assertEqual(self.notifications[-1]["voice_state"], "ready")
        # A subsequent request retains these same objects and needs no reload.
        self.assertEqual(self.policy(reclaim).check(trigger="idle_pressure")["reclaimed"], False)

    def test_releasing_vram_without_host_commit_gain_requires_explicit_unload(self):
        def reclaim():
            self.state["gpu_reserved_bytes"] = self.state["gpu_allocated_bytes"]
            return {}
        result = self.policy(reclaim).check(trigger="idle_pressure")
        self.assertEqual(result["action"], "unload")
        self.assertEqual(result["cache_reclaim"]["global_commit_change_bytes"], 0)
        self.assertEqual(self.notifications[-1]["voice_state"], "unloaded_for_resources")
        self.assertEqual(result["resource_unload"]["category"], "host_commit")
        self.assertTrue(result["resource_unload"]["retryable"])

    def test_close_during_reclaim_does_not_reannounce_ready_or_start_work(self):
        def reclaim():
            self.closed = True
            self.state["commit_available_bytes"] = int(1.75 * GIB)
            return {}
        result = self.policy(reclaim).check(trigger="post_request_pressure")
        self.assertEqual(result["action"], "closed")
        self.assertFalse(any(event["voice_state"] == "ready" for event in self.notifications))
        self.notifications.clear()
        self.assertEqual(self.policy(reclaim).check(trigger="idle_pressure")["action"], "closed")
        self.assertEqual(self.notifications, [])

    def test_late_delivered_result_does_not_hide_a_resource_unload(self):
        from .backend import ResidentBackend
        backend = ResidentBackend(self.config)
        backend._process = SimpleNamespace(poll=lambda: 72, returncode=72)
        backend._state.update(voice_state="unloaded_for_resources", backend_stage="unloaded_for_resources", resident=False)
        backend._settle_result({"status": "generated", "initialization_complete": True})
        health = backend.health()
        self.assertEqual(health["voice_state"], "unloaded_for_resources")
        self.assertEqual(health["backend_stage"], "unloaded_for_resources")
        self.assertFalse(health["resident"])

    def test_grace_covers_normal_twenty_second_admission_without_reclaim_loop(self):
        from .resources import admission_requirements
        self.config["resources"]["admission_wait_seconds"] = 20
        self.config["resources"]["idle_grace_extra_seconds"] = 2
        now, calls = [0.0], []
        def reclaim():
            calls.append(True)
            self.state["gpu_reserved_bytes"] = self.state["gpu_allocated_bytes"]
            return {}
        policy = IdlePressurePolicy(self.config, snapshot=lambda: copy.deepcopy(self.state), reclaim=reclaim,
                                    notify=self.notifications.append, clock=lambda: now[0])
        initial = policy.check(trigger="post_request_pressure")
        self.assertEqual(initial["action"], "keep")
        self.assertEqual(initial["resource_grace"]["total_seconds"], 22)
        for timestamp in (6.0, 19.0, 21.5):
            now[0] = timestamp
            self.assertEqual(policy.check(trigger="idle_pressure")["action"], "keep")
        self.assertEqual(len(calls), 1)
        # A following sentence still has to meet the unchanged 1.5 GiB
        # normal admission; grace itself grants no permission to synthesize.
        required = admission_requirements(self.config, cold=False)
        self.assertEqual(required["commit_available_bytes"], int(1.5 * GIB))
        self.assertLess(self.state["commit_available_bytes"], required["commit_available_bytes"])
        self.state["commit_available_bytes"] = int(1.6 * GIB)
        policy.note_request_admitted()
        self.assertIsNone(policy.grace_until)

    def test_grace_is_not_renewed_when_it_expires_without_admitted_work(self):
        self.config["resources"]["admission_wait_seconds"] = 20
        self.config["resources"]["idle_grace_extra_seconds"] = 2
        now, calls = [0.0], []
        def reclaim():
            calls.append(True)
            return {}
        policy = IdlePressurePolicy(self.config, snapshot=lambda: copy.deepcopy(self.state), reclaim=reclaim,
                                    notify=self.notifications.append, clock=lambda: now[0])
        policy.check(trigger="post_request_pressure")
        now[0] = 22.1
        result = policy.check(trigger="idle_pressure")
        self.assertEqual(result["action"], "unload")
        self.assertEqual(result["resource_unload"]["reason"], "idle_resource_grace_expired")
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
