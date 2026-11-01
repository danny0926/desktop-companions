"""Read-only resource checks shared by the facade and its owned ML process."""
from __future__ import annotations

import ctypes
import os
import subprocess
import json
import struct
import time
import gc
from pathlib import Path

GIB = 1024 ** 3
_gpu_sample: tuple[float, dict] = (0.0, {})


def memory_status() -> dict:
    if os.name != "nt":
        return {"resource_probe_error": "The desktop resource guard requires Windows."}

    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in
            ("total_phys", "avail_phys", "total_commit", "avail_commit", "total_virtual", "avail_virtual", "extended")
        ]

    status = Status()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError()
    return {"physical_available_bytes": int(status.avail_phys),
            "commit_available_bytes": int(status.avail_commit),
            "physical_total_bytes": int(status.total_phys)}


def gpu_status(*, refresh: bool = False) -> dict:
    global _gpu_sample
    now = time.monotonic()
    if not refresh and now - _gpu_sample[0] < 5.0:
        return dict(_gpu_sample[1])
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        free, total = result.stdout.splitlines()[0].split(",")
        value = {"gpu_free_bytes": int(free.strip()) * 1024 ** 2,
                 "gpu_total_bytes": int(total.strip()) * 1024 ** 2}
    except (OSError, ValueError, IndexError, subprocess.SubprocessError) as exc:
        value = {"gpu_probe_error": str(exc)}
    _gpu_sample = (now, value)
    return dict(value)


class ResourceBlocked(RuntimeError):
    def __init__(self, stage: str, memory: dict, required: dict, *, phase=None, waited_seconds=0.0):
        super().__init__(f"Insufficient resource headroom before {stage}.")
        self.details = resource_details(stage, memory, required, phase=phase, elapsed=waited_seconds)
        self.details.update(waited_seconds=waited_seconds, retryable=True,
                            action="retry_voice_when_capacity_is_available")


class ResourceWaitCancelled(RuntimeError):
    pass


def resource_details(stage: str, memory: dict, required: dict, *, phase=None, elapsed=0.0, timeout=0.0) -> dict:
    shortfalls = {key: max(0, amount - memory.get(key, 0)) for key, amount in required.items()}
    labels = {"physical_available_bytes": "host_ram", "commit_available_bytes": "host_commit", "gpu_free_bytes": "gpu_memory"}
    missing = [key for key in required if key not in memory]
    primary = next((key for key, amount in shortfalls.items() if amount), None)
    category = "probe" if missing else labels.get(primary, "probe")
    return {"stage": stage, "phase": phase or ("cold_load" if stage == "cold_load" else "warm_synthesis"),
            "category": category, "primary_key": primary, "observed": memory, "required": required,
            "shortfall_bytes": shortfalls.get(primary, 0), "shortfalls_bytes": shortfalls,
            "elapsed_seconds": elapsed, "timeout_seconds": timeout, "retryable": True}


def wait_for_resources(probe, required: dict, *, stage: str, phase: str, timeout_seconds: float,
                       cancelled=lambda: False, on_wait=lambda value: None, interval_seconds=0.25) -> dict:
    """Bounded and cancellable admission; never changes another process."""
    started = time.monotonic()
    while True:
        if cancelled():
            raise ResourceWaitCancelled("Resource wait cancelled before compute.")
        memory = probe()
        if all(memory.get(key, -1) >= amount for key, amount in required.items()):
            on_wait(None)
            return memory
        elapsed = time.monotonic() - started
        details = resource_details(stage, memory, required, phase=phase, elapsed=elapsed, timeout=timeout_seconds)
        on_wait(details)
        if elapsed >= timeout_seconds:
            error = ResourceBlocked(stage, memory, required, phase=phase, waited_seconds=elapsed)
            error.details["timeout_seconds"] = timeout_seconds
            raise error
        time.sleep(min(interval_seconds, max(0.0, timeout_seconds - elapsed)))


def release_cold_stage_memory(*, snapshot=memory_status) -> dict:
    """Collect free buffers and trim only the calling ML worker's cold pages.

    Live model tensors remain on CUDA. This does not change global Windows
    memory settings or any other process. Pages used again can fault back in,
    so subsequent stages still check real headroom and retain the watchdog.
    """
    before = snapshot()
    collected = gc.collect()
    result = {"scope": "current_voice_worker_only", "collected_objects": collected, "before": before}
    if os.name == "nt":
        try:
            result["heapmin_return"] = int(ctypes.CDLL("ucrtbase")._heapmin())
        except (OSError, AttributeError) as exc:
            result["heapmin_error"] = str(exc)
        empty = ctypes.windll.kernel32.K32EmptyWorkingSet
        empty.argtypes = [ctypes.c_void_p]
        result["working_set_trimmed"] = bool(empty(ctypes.c_void_p(-1)))
    result["after"] = snapshot()
    return result


def staged_cold_requirement(config: dict) -> dict:
    """Admission budget for direct-tensor loading, not a lower watchdog reserve.

    The import component was sampled on this host (583 MB resident for torch,
    Irodori and SilentCipher, without CUDA/models). The largest FP32 source
    tensor is derived from the pinned checkpoint header, without loading it.
    Stage checks and the independent host-reserve watchdog remain mandatory.
    """
    checkpoint = Path(config["model_path"])
    header_available = checkpoint.is_file()
    # A clean checkout can report admission/health without owning ML weights.
    # Construction still requires every artifact before launching the worker.
    largest = int(config["checkpoint_transfer_chunk_mib"]) * 1024 ** 2
    if header_available:
        with checkpoint.open("rb") as stream:
            size = struct.unpack("<Q", stream.read(8))[0]
            if not 0 < size < 16 * 1024 ** 2:
                raise ValueError("Invalid pinned safetensors header size.")
            header = json.loads(stream.read(size))
        largest = max(entry["data_offsets"][1] - entry["data_offsets"][0]
                      for name, entry in header.items() if name != "__metadata__")
    budget = config["resources"]["staged_loading_budget_gib"]
    components = {key: int(value * GIB) for key, value in budget.items()}
    components["largest_source_tensor"] = min(largest, int(config["checkpoint_transfer_chunk_mib"]) * 1024 ** 2)
    # The new loader checks every stage after releasing its predecessor's
    # temporary allocations. Admission covers only imports plus the unchanged
    # reserve; later codec/adapter/watermark work is checked when it occurs.
    required = sum(components.values())
    if config["resources"].get("loading_policy") == "bounded_stages":
        required = components["host_reserve"] + components["imports"]
    return {"required_physical_bytes": required, "components_bytes": components,
            "largest_checkpoint_tensor_bytes": largest if header_available else None,
            "checkpoint_header_available": header_available,
            "policy": config["resources"]["loading_policy"], "single_source_tensor": True,
            "source_tensor_is_transferred_in_slabs": True}


def admission_requirements(config: dict, *, cold: bool) -> dict:
    limits = config["resources"]
    required = {
        "physical_available_bytes": int(limits["cold_min_free_ram_gib" if cold else "warm_min_free_ram_gib"] * GIB),
        "commit_available_bytes": int(limits["cold_min_free_commit_gib" if cold else "warm_min_free_commit_gib"] * GIB),
        "gpu_free_bytes": int(limits["cold_min_free_gpu_gib" if cold else "warm_min_free_gpu_gib"] * GIB),
    }
    if cold and limits.get("loading_policy") in {"direct_tensor_stage_budget", "bounded_stages"}:
        admission = staged_cold_requirement(config)
        required["physical_available_bytes"] = admission["required_physical_bytes"]
    return required


def preflight(config: dict, *, cold: bool) -> dict:
    memory = {**memory_status(), **gpu_status(refresh=cold)}
    required = admission_requirements(config, cold=cold)
    if cold:
        memory["cold_admission"] = staged_cold_requirement(config)
    if any(memory.get(key, -1) < amount for key, amount in required.items()):
        raise ResourceBlocked("cold_load" if cold else "warm_synthesis", memory, required,
                              phase="cold_load" if cold else "warm_synthesis")
    return memory
