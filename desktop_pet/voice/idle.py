"""Injected, stdlib-only policy for reclaiming an idle worker's unused cache."""
from __future__ import annotations

import time

from .resources import GIB, resource_details


class IdlePressurePolicy:
    def __init__(self, config: dict, *, snapshot, reclaim, notify=lambda value: None, closing=lambda: False, clock=time.monotonic):
        self.config, self.snapshot, self.reclaim = config, snapshot, reclaim
        self.notify, self.closing = notify, closing
        self.clock = clock
        self.grace_until = None

    def note_request_admitted(self):
        """Only call after the unchanged normal admission accepted new work."""
        self.grace_until = None

    def _grace(self, required):
        return {"active": True, "remaining_seconds": max(0.0, self.grace_until - self.clock()),
                "total_seconds": self.config["resources"]["admission_wait_seconds"] + self.config["resources"]["idle_grace_extra_seconds"],
                "normal_admission_required": required, "normal_admission_unchanged": True}

    def _unload(self, observed, required, reason):
        unload = resource_details("idle_pressure", observed, required, phase="idle")
        unload.update(reason=reason, retryable=True)
        self.notify({"voice_state": "unloaded_for_resources", "backend_stage": "unloaded_for_resources",
                     "resident": False, "resource_unload": unload, "resource_grace": {"active": False}})
        return {"action": "unload", "reclaimed": False, "resource_unload": unload}

    def check(self, *, trigger: str) -> dict:
        if self.closing():
            return {"action": "closed"}
        before = self.snapshot()
        limits = self.config["resources"]
        required = {"physical_available_bytes": int(limits["idle_keep_free_ram_gib"] * GIB),
                    "commit_available_bytes": int(limits["idle_keep_free_commit_gib"] * GIB)}
        margin_required = {
            "physical_available_bytes": int((limits["worker_floor_ram_gib"] + limits["idle_grace_ram_margin_gib"]) * GIB),
            "commit_available_bytes": int((limits["worker_floor_commit_gib"] + limits["idle_grace_commit_margin_gib"]) * GIB)}
        if all(before.get(key, 0) >= amount for key, amount in required.items()):
            if self.grace_until is not None:
                self.notify({"voice_state": "ready", "resource_grace": None})
            self.grace_until = None
            return {"action": "keep", "reclaimed": False}
        if self.grace_until is not None:
            if not all(before.get(key, 0) >= amount for key, amount in margin_required.items()):
                return self._unload(before, margin_required, "idle_pressure_below_grace_margin")
            if self.clock() >= self.grace_until:
                return self._unload(before, required, "idle_resource_grace_expired")
            grace = self._grace(required)
            self.notify({"voice_state": "ready", "resource_grace": grace})
            return {"action": "keep", "reclaimed": False, "resource_grace": grace}
        self.notify({"voice_state": "reclaiming_cache", "backend_stage": "reclaiming_cache",
                     "cache_reclaim": {"trigger": trigger, "phase": "idle", "before": before, "in_progress": True}})
        details = self.reclaim()
        after = self.snapshot()
        report = {"trigger": trigger, "phase": "idle", "before": before, "after": after,
                  "gpu_cache_released_bytes": max(0, before.get("gpu_reserved_bytes", 0) - after.get("gpu_reserved_bytes", 0)),
                  "gpu_allocated_change_bytes": after.get("gpu_allocated_bytes", 0) - before.get("gpu_allocated_bytes", 0),
                  "global_commit_change_bytes": after.get("commit_available_bytes", 0) - before.get("commit_available_bytes", 0),
                  "global_ram_change_bytes": after.get("physical_available_bytes", 0) - before.get("physical_available_bytes", 0),
                  "model_preserved": True, "in_progress": False, **details}
        if self.closing():
            return {"action": "closed", "cache_reclaim": report}
        if all(after.get(key, 0) >= amount for key, amount in required.items()):
            self.grace_until = None
            self.notify({"voice_state": "ready", "backend_stage": "ready", "cache_reclaim": report,
                         "resource_unload": None, "resource_grace": None})
            return {"action": "keep", "reclaimed": True, "cache_reclaim": report}
        grace_seconds = float(limits["admission_wait_seconds"]) + float(limits["idle_grace_extra_seconds"])
        if grace_seconds > 0 and all(after.get(key, 0) >= amount for key, amount in margin_required.items()):
            self.grace_until = self.clock() + grace_seconds
            grace = self._grace(required)
            self.notify({"voice_state": "ready", "backend_stage": "ready", "cache_reclaim": report,
                         "resource_unload": None, "resource_grace": grace})
            return {"action": "keep", "reclaimed": True, "cache_reclaim": report, "resource_grace": grace}
        result = self._unload(after, margin_required if grace_seconds > 0 else required, "idle_pressure_after_cache_reclaim")
        result["cache_reclaim"] = report
        return result
