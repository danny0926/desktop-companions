"""Small, thread-safe facade for one resident Irodori process and one FIFO.

The UI calls begin_turn() when input starts, stops its playback itself, and
calls synthesize() on a background thread. A WAV is exposed only for the active
turn. This API is sentence pipelining, not native acoustic streaming.
"""
from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config/desktop_pet_voice.json"
TERMINAL = {"generated", "ready", "cancelled", "stale_turn", "queue_full", "expired", "blocked_memory", "failed", "closed"}


def load_config(path: str | Path | None = None) -> dict:
    config_path = Path(path) if path is not None else DEFAULT_CONFIG
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    local_path = config_path.with_suffix(".local.json")
    if local_path.is_file():
        overrides = json.loads(local_path.read_text(encoding="utf-8-sig"))
        if not isinstance(overrides, dict):
            raise ValueError("The local voice config must be a JSON object.")
        config.update(overrides)
    config["_config_path"] = str(config_path.resolve())
    for key in ("python", "adapter_path", "reference_latent", "output_dir", "upstream_dir",
                "hf_home", "model_path", "codec_path", "watermark_dir"):
        value = Path(config[key])
        config[key] = str((value if value.is_absolute() else ROOT / value).resolve())
    if not 1 <= int(config["queue_capacity"]) <= 32:
        raise ValueError("queue_capacity must be between 1 and 32.")
    return config


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(f".{uuid.uuid4().hex}.part")
    part.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    part.replace(path)


@dataclass
class _Task:
    request: dict
    result: dict
    submitted: float = field(default_factory=time.perf_counter)
    done: threading.Event = field(default_factory=threading.Event)
    cancel: threading.Event = field(default_factory=threading.Event)
    started: float | None = None
    warmup: bool = False


class VoiceService:
    """A single, lazy ML worker. All public methods are safe from UI threads.

    synthesize(VoiceRequest | dict, timeout=None) -> result dictionary.
    submit(...) -> request_id; wait/get_request expose the same result schema.
    begin_turn invalidates every previous turn; it does not stop audio devices.
    A caller timeout cancels compute and returns status=timeout immediately;
    the worker's final cancelled/expired status remains available by request_id.
    """

    def __init__(self, config_path: str | Path | None = None, *, backend: Any = None, profile: str | None = None):
        self.config = load_config(config_path)
        self._profile = profile or self.config["default_profile"]
        self._check_profile(self._profile)
        if backend is None:
            from .backend import ResidentBackend
            backend = ResidentBackend(self.config)
        self._backend = backend
        self._condition = threading.Condition(threading.RLock())
        self._pending: deque[_Task] = deque()
        self._tasks: OrderedDict[str, _Task] = OrderedDict()
        self._active: _Task | None = None
        self._turn: str | None = None
        self._closed = False
        self._worker = threading.Thread(target=self._run, name="pet-voice-queue", daemon=True)
        self._worker.start()

    def _request(self, request: Any) -> dict:
        if hasattr(request, "to_dict"):
            request = request.to_dict()
        if not isinstance(request, dict):
            raise TypeError("Expected VoiceRequest or a request dictionary.")
        result = {key: request.get(key, "0" if key == "segment_id" else "")
                  for key in ("text_ja", "caption_ja", "turn_id", "segment_id")}
        for key, value in result.items():
            if not isinstance(value, str) or (key != "caption_ja" and not value.strip()):
                raise ValueError(f"{key} must be a nonempty string.")
        result["text_ja"] = result["text_ja"].strip()
        if len(result["text_ja"]) > int(self.config["max_text_chars"]):
            raise ValueError("The utterance is too long; split it into short Japanese sentences.")
        if len(result["caption_ja"]) > int(self.config["max_caption_chars"]):
            raise ValueError("caption_ja exceeds the configured limit.")
        if any(len(result[key]) > 128 for key in ("turn_id", "segment_id")):
            raise ValueError("turn_id and segment_id must be at most 128 characters.")
        return result

    def begin_turn(self, turn_id: str) -> dict:
        if not isinstance(turn_id, str) or not turn_id.strip() or len(turn_id) > 128:
            raise ValueError("turn_id must be a nonempty string of at most 128 characters.")
        active_id = None
        with self._condition:
            if self._closed:
                return {"status": "closed", "turn_id": turn_id}
            previous = self._turn
            self._turn = turn_id
            cancelled = []
            for request_id, task in self._tasks.items():
                if task.warmup:
                    continue
                if task.request["turn_id"] != turn_id:
                    if task.result["status"] not in TERMINAL or task.result["status"] == "generated":
                        cancelled.append(request_id)
                        task.cancel.set()
                        task.result["compute_state"] = "cancel_requested" if task is self._active else "stopped"
                        self._finish(task, "stale_turn")
            self._pending = deque(task for task in self._pending if not task.cancel.is_set())
            if self._active is not None and self._active.cancel.is_set():
                active_id = self._active.result["request_id"]
            self._condition.notify_all()
        if active_id:
            self._backend.cancel(active_id)
        return {"status": "active", "turn_id": turn_id, "previous_turn_id": previous,
                "discarded_request_ids": cancelled, "playback_action": "caller_must_stop_playback"}

    def _check_profile(self, profile: str) -> str:
        if profile not in self.config["profiles"]:
            raise ValueError(f"Unknown voice profile: {profile}")
        return profile

    def set_profile(self, profile: str) -> dict:
        profile = self._check_profile(profile)
        with self._condition:
            self._profile = profile
        return {"profile": profile, **self.config["profiles"][profile], "reload_required": False}

    def submit(self, request: Any, *, profile: str | None = None) -> str:
        request = self._request(request)
        with self._condition:
            request["synthesis_profile"] = self._check_profile(profile or self._profile)
        return self._enqueue(request)

    def submit_prewarm(self) -> str:
        return self._enqueue({"text_ja": "", "caption_ja": "", "turn_id": "", "segment_id": "0",
                              "_op": "prewarm"}, warmup=True)

    def prewarm(self, timeout: float | None = None) -> dict:
        """Load model/codec/tokenizers/LoRA/reference/watermark; generate no audio.

        Call on a background thread, or use submit_prewarm()+get_request().
        This does not execute sampling or warm every possible CUDA input shape.
        """
        return self.wait(self.submit_prewarm(), timeout=timeout)

    def _enqueue(self, request: dict, *, warmup: bool = False) -> str:
        request_id = uuid.uuid4().hex
        public_request = {key: value for key, value in request.items() if not key.startswith("_")}
        result = {"schema": "desktop-pet-voice-result-v1", "request_id": request_id, **public_request,
                  "status": "queued", "wav_path": None, "playable": False,
                  "backend": self.config["backend"], "adapter_path": self.config["adapter_path"],
                  "adapter_sha256": self.config["adapter_sha256"], "timings_seconds": {},
                  "created_utc": datetime.now(timezone.utc).isoformat(),
                  "metadata_path": str(Path(self.config["output_dir"]) / "requests" / f"{request_id}.json"),
                  "streaming": "complete_sentence_wav", "watermark_complete": False,
                  "operation": "prewarm" if warmup else "synthesize"}
        result["compute_state"] = "queued"
        task = _Task(request, result, warmup=warmup)
        with self._condition:
            self._prune()
            self._tasks[request_id] = task
            if self._closed:
                self._finish(task, "closed")
            elif not warmup and self._turn is not None and self._turn != request["turn_id"]:
                self._finish(task, "stale_turn")
            elif len(self._pending) >= int(self.config["queue_capacity"]):
                self._finish(task, "queue_full")
            else:
                if self._turn is None and not warmup:
                    self._turn = request["turn_id"]
                self._pending.append(task)
                self._condition.notify()
        return request_id

    def synthesize(self, request: Any, timeout: float | None = None, *, profile: str | None = None) -> dict:
        return self.wait(self.submit(request, profile=profile), timeout=timeout)

    def wait(self, request_id: str, timeout: float | None = None) -> dict:
        with self._condition:
            task = self._tasks[request_id]
        if not task.done.wait(timeout):
            self.cancel(request_id=request_id)
            result = self.get_request(request_id)
            result["status"] = "timeout"
            result["compute_cancel_requested"] = True
            return result
        return self.get_request(request_id)

    def get_request(self, request_id: str) -> dict:
        with self._condition:
            task = self._tasks[request_id]
            result = copy.deepcopy(task.result)
            if task is self._active and not task.done.is_set():
                backend = self._backend.health()
                waiting = backend.get("resource_wait")
                if backend.get("backend_stage") == "waiting_resources" and waiting and waiting.get("request_id") == request_id:
                    result.update(status="waiting_resources", resource_wait=waiting)
            if result["status"] == "generated" and task.request["turn_id"] != self._turn:
                result.update(status="stale_turn", wav_path=None, playable=False)
            return result

    def cancel(self, *, turn_id: str | None = None, request_id: str | None = None) -> dict:
        active_id = None
        with self._condition:
            cancelled = []
            for task_id, task in self._tasks.items():
                if request_id is not None and task_id != request_id:
                    continue
                if turn_id is not None and task.request["turn_id"] != turn_id:
                    continue
                if task.result["status"] in TERMINAL and task.result["status"] != "generated":
                    continue
                task.cancel.set()
                task.result["compute_state"] = "cancel_requested" if task is self._active else "stopped"
                self._finish(task, "cancelled")
                cancelled.append(task_id)
                if task is self._active:
                    active_id = task_id
            self._pending = deque(task for task in self._pending if not task.cancel.is_set())
            self._condition.notify_all()
        if active_id:
            self._backend.cancel(active_id)
        return {"status": "cancel_requested", "request_ids": cancelled,
                "compute": "cooperative_at_model_call_and_stage_boundaries",
                "playback_action": "caller_must_stop_playback"}

    def health(self) -> dict:
        from .resources import memory_status
        with self._condition:
            state = {"status": "closed" if self._closed else "ok", "active_turn_id": self._turn,
                     "queue_depth": len(self._pending), "queue_capacity": int(self.config["queue_capacity"]),
                     "active_request_id": self._active.result["request_id"] if self._active else None,
                     "profile": self._profile, "profiles": copy.deepcopy(self.config["profiles"]),
                     "streaming": "complete_sentence_wav", "voice_input": self.config["voice_input"],
                     "resources": memory_status()}
        state.update(self._backend.health())
        return state

    def close(self) -> None:
        active_id = None
        with self._condition:
            if self._closed:
                return
            self._closed = True
            for task in self._tasks.values():
                if task.result["status"] not in TERMINAL:
                    task.cancel.set()
                    task.result["compute_state"] = "cancel_requested" if task is self._active else "stopped"
                    self._finish(task, "closed")
                    if task is self._active:
                        active_id = task.result["request_id"]
            self._pending.clear()
            self._condition.notify_all()
        if active_id:
            self._backend.cancel(active_id)
        self._backend.close()
        self._worker.join(timeout=3.0)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _finish(self, task: _Task, status: str, extra: dict | None = None) -> None:
        if extra:
            task.result.update(extra)
        task.result["status"] = status
        task.result["timings_seconds"]["full_from_submit"] = time.perf_counter() - task.submitted
        task.result["playable"] = status == "generated"
        if task.started is None and status != "generated":
            task.result["compute_state"] = "stopped"
        if status != "generated":
            if task.result.get("wav_path"):
                task.result["discarded_wav_path"] = task.result["wav_path"]
            task.result["wav_path"] = None
        try:
            atomic_json(Path(task.result["metadata_path"]), task.result)
        except OSError as exc:
            task.result["metadata_error"] = str(exc)
        task.done.set()

    def _prune(self):
        limit = int(self.config.get("retained_request_count", 128))
        for key, task in list(self._tasks.items()):
            if len(self._tasks) < limit:
                break
            if task.done.is_set() and task is not self._active:
                del self._tasks[key]

    def _run(self):
        from .resources import ResourceBlocked
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or bool(self._pending))
                if self._closed:
                    return
                task = self._pending.popleft()
                if task.cancel.is_set():
                    continue
                task.started = time.perf_counter()
                task.result["timings_seconds"]["queue"] = task.started - task.submitted
                if task.started - task.submitted > float(self.config["queue_max_age_seconds"]):
                    self._finish(task, "expired")
                    continue
                task.result["status"] = "running"
                task.result["compute_state"] = "running"
                self._active = task
            try:
                response = self._backend.synthesize({**task.request, "request_id": task.result["request_id"]}, task.cancel)
            except ResourceBlocked as exc:
                response = {"status": "blocked_memory", "resource_block": exc.details}
            except Exception as exc:
                response = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            with self._condition:
                timings = task.result["timings_seconds"]
                timings.update(response.pop("timings_seconds", {}))
                stale = not task.warmup and task.request["turn_id"] != self._turn
                if task.cancel.is_set() or stale:
                    status = "stale_turn" if stale else "cancelled"
                else:
                    status = response.pop("status", "failed")
                response.pop("status", None)
                if status == "generated":
                    timings["first_playable_from_submit"] = time.perf_counter() - task.submitted
                task.result["compute_state"] = "finished"
                self._finish(task, status, response)
                self._active = None
