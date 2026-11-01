"""The stdlib process bridge; exactly one owned ML process is reused."""
from __future__ import annotations

import json
import ctypes
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .resources import (ResourceBlocked, ResourceWaitCancelled, admission_requirements,
                        gpu_status, memory_status, resource_details, wait_for_resources)
from .service import ROOT


class _OwnedWindowsJob:
    """Windows venv launchers spawn a child; keep the entire owned tree bounded."""
    def __init__(self):
        self.handle = None
        self.lock = threading.Lock()
        if os.name != "nt":
            return
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(key, ctypes.c_uint64) for key in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        kernel = ctypes.windll.kernel32
        kernel.CreateJobObjectW.restype = ctypes.c_void_p
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel.SetInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL)]
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.handle = kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError()
        limits = Extended()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel.CloseHandle(self.handle)
            self.handle = None
            raise ctypes.WinError()

    def attach(self, pid: int):
        if os.name != "nt":
            return
        from ctypes import wintypes
        with self.lock:
            if not self.handle:
                return
            kernel = ctypes.windll.kernel32
            process = kernel.OpenProcess(0x1101, False, pid)
            if not process:
                raise ctypes.WinError()
            try:
                present = wintypes.BOOL()
                if not kernel.IsProcessInJob(process, self.handle, ctypes.byref(present)):
                    raise ctypes.WinError()
                if not present and not kernel.AssignProcessToJobObject(self.handle, process):
                    raise ctypes.WinError()
            finally:
                kernel.CloseHandle(process)

    def close(self):
        with self.lock:
            if self.handle:
                ctypes.windll.kernel32.CloseHandle(self.handle)
                self.handle = None


class ResidentBackend:
    def __init__(self, config: dict):
        self.config = config
        self._process: subprocess.Popen | None = None
        self._events: queue.Queue = queue.Queue()
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state = {"resident": False, "worker_pid": None, "runtime_load_count": 0,
                       "backend_stage": "not_loaded", "voice_state": "not_loaded", "backend": config["backend"]}
        self._log = None
        self._closing = False
        self._job = None

    def health(self) -> dict:
        with self._state_lock:
            state = dict(self._state)
        if self._process is not None and self._process.poll() is not None:
            state.update(resident=False, worker_exit_code=self._process.returncode)
            if state.get("voice_state") == "unloaded_for_resources" or self._process.returncode == 72:
                state.update(voice_state="unloaded_for_resources", backend_stage="unloaded_for_resources")
            else:
                state["backend_stage"] = "worker_exited"
        return state

    def _admit(self, request: dict, cancel_event: threading.Event, *, cold: bool):
        phase = "cold_load" if cold else "warm_synthesis"
        required = admission_requirements(self.config, cold=cold)
        def progress(details):
            with self._state_lock:
                self._state.pop("resource_block", None)
                self._state["resource_wait"] = {**details, "request_id": request["request_id"]} if details else None
                self._state["backend_stage"] = "waiting_resources" if details else ("starting" if cold else "ready")
        try:
            return wait_for_resources(lambda: {**memory_status(), **gpu_status()}, required,
                                      stage="process_start" if cold else "warm_synthesis", phase=phase,
                                      timeout_seconds=float(self.config["resources"]["admission_wait_seconds"]),
                                      cancelled=cancel_event.is_set, on_wait=progress)
        except ResourceBlocked as exc:
            with self._state_lock:
                self._state.update(backend_stage="blocked_memory", resource_block=exc.details, resource_wait=None)
            raise
        except ResourceWaitCancelled:
            with self._state_lock:
                self._state.update(backend_stage="not_loaded" if cold else "ready", resource_wait=None)
            raise

    def _start(self, request: dict, cancel_event: threading.Event):
        if self._closing:
            raise RuntimeError("The voice backend is closed.")
        if self._state.get("voice_state") == "unloaded_for_resources" and self._process is not None:
            self._stop_owned_process()
        if self._process is not None and self._process.poll() is None:
            return
        if self._process is not None:
            for stream in (self._process.stdin, self._process.stdout):
                if stream:
                    stream.close()
            self._process = None
        with self._state_lock:
            self._state["voice_state"] = "loading"
        self._admit(request, cancel_event, cold=True)
        from runtime.voice_artifacts import require_artifacts
        require_artifacts(self.config)
        python = Path(self.config["python"])
        if not python.is_file():
            raise FileNotFoundError(f"Existing Irodori virtualenv Python not found: {python}")
        log_dir = Path(self.config["output_dir"]) / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        if self._log:
            self._log.close()
        log_path = log_dir / f"worker-{uuid.uuid4().hex}.log"
        self._log = log_path.open("ab", buffering=0)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self._events = queue.Queue()
        if self._job:
            self._job.close()
        self._job = _OwnedWindowsJob()
        self._process = subprocess.Popen(
            [str(python), "-u", "-m", "desktop_pet.voice.worker", "--config", self.config["_config_path"]],
            cwd=str(ROOT), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self._log, text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            self._job.attach(self._process.pid)
        except OSError:
            self._process.terminate()
            self._process.wait(timeout=3)
            self._job.close()
            raise
        with self._state_lock:
            self._state.update(worker_pid=None, launcher_pid=self._process.pid, backend_stage="starting", worker_log=str(log_path),
                               runtime_load_count=0, resident=False, voice_state="loading", resource_unload=None)
        threading.Thread(target=self._read, args=(self._process, self._events, self._job),
                         name="pet-voice-protocol", daemon=True).start()

    def _read(self, process: subprocess.Popen, events: queue.Queue, job):
        assert process.stdout is not None
        try:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "boot":
                    try:
                        job.attach(int(event["worker_pid"]))
                        with self._state_lock:
                            self._state["worker_pid"] = int(event["worker_pid"])
                    except OSError as exc:
                        events.put({"event": "fatal", "error": f"Unable to bound the owned worker process tree: {exc}"})
                        job.close()
                        continue
                if event.get("event") in {"stage", "ready", "session", "maintenance", "resource_unloaded"}:
                    with self._state_lock:
                        health = dict(event.get("health", {}))
                        if event.get("event") == "maintenance" and self._state.get("resource_wait") and health.get("backend_stage") == "ready":
                            health.pop("backend_stage")
                        self._state.update(health)
                events.put(event)
        finally:
            events.put({"event": "eof"})

    def _send(self, command: dict) -> bool:
        with self._write_lock:
            process = self._process
            if process is None or process.poll() is not None or process.stdin is None:
                return False
            try:
                process.stdin.write(json.dumps(command, ensure_ascii=False) + "\n")
                process.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                return False

    def cancel(self, request_id: str):
        self._send({"op": "cancel", "request_id": request_id})

    def _settle_result(self, result: dict) -> dict:
        with self._state_lock:
            initialized = result.get("initialization_complete", self._state.get("resident", False))
        if result.get("status") == "failed" and not initialized:
            # A failed construction may have imported incompatible code or
            # partially changed modules. A retry needs a fresh owned process.
            self._stop_owned_process()
            result.update(worker_disposed=True, retryable=True)
        with self._state_lock:
            self._state["resource_wait"] = None
            if self._state.get("voice_state") == "unloaded_for_resources":
                self._state.update(backend_stage="unloaded_for_resources", resident=False)
            elif result.get("status") == "blocked_memory":
                self._state.update(backend_stage="blocked_memory", resource_block=result.get("resource_block"))
            else:
                self._state.update(backend_stage="ready" if self._state.get("resident") else "not_loaded", resource_block=None)
        return result

    def synthesize(self, request: dict, cancel_event: threading.Event) -> dict:
        started = time.perf_counter()
        try:
            self._start(request, cancel_event)
        except ResourceWaitCancelled:
            return {"status": "cancelled", "compute_state": "finished"}
        if cancel_event.is_set():
            return {"status": "cancelled"}
        try:
            self._admit(request, cancel_event, cold=False)
        except ResourceWaitCancelled:
            return {"status": "cancelled", "compute_state": "finished"}
        if not self._send({"op": "synthesize", "request": request}):
            raise RuntimeError("The resident voice worker is unavailable.")
        sent_cancel = False
        timeout = float(self.config["request_timeout_seconds"])
        deadline = time.monotonic() + timeout
        while True:
            if cancel_event.is_set() and not sent_cancel:
                self.cancel(request["request_id"])
                sent_cancel = True
            if time.monotonic() > deadline:
                self.cancel(request["request_id"])
                # A blocked driver cannot keep the UI/queue stuck forever. Only
                # the Popen created above is eligible for termination here.
                self._stop_owned_process()
                return {"status": "failed", "error": "The owned TTS worker exceeded its request timeout.",
                        "timings_seconds": {"backend_roundtrip": time.perf_counter() - started}}
            try:
                event = self._events.get(timeout=0.1)
            except queue.Empty:
                continue
            if event.get("event") == "result" and event.get("request_id") == request["request_id"]:
                result = self._settle_result(event["result"])
                result.setdefault("timings_seconds", {})["backend_roundtrip"] = time.perf_counter() - started
                return result
            if event.get("event") == "fatal":
                raise RuntimeError(event.get("error", "The resident worker failed."))
            if event.get("event") == "resource_unloaded":
                details = event.get("health", {}).get("resource_unload")
                self._stop_owned_process()
                with self._state_lock:
                    self._state.update(voice_state="unloaded_for_resources", backend_stage="unloaded_for_resources",
                                       resource_unload=details, resident=False)
                return {"status": "blocked_memory", "worker_unloaded": True, "retryable": True,
                        "resource_block": details, "reason": "idle_resource_unload_before_next_request"}
            if event.get("event") == "eof":
                process = self._process
                exit_code = process.wait(timeout=2) if process else None
                if exit_code == 72:
                    with self._state_lock:
                        details = self._state.get("resource_unload")
                    self._stop_owned_process()
                    with self._state_lock:
                        self._state.update(voice_state="unloaded_for_resources", backend_stage="unloaded_for_resources",
                                           resource_unload=details, resident=False)
                    return {"status": "blocked_memory", "worker_unloaded": True, "retryable": True,
                            "resource_block": details, "reason": "idle_resource_unload_before_next_request"}
                if exit_code == 71:
                    with self._state_lock:
                        pid = self._state.get("worker_pid")
                    aborts = sorted(Path(self.config["output_dir"]).glob(f"memory-abort-{pid}-*.json"),
                                    key=lambda path: path.stat().st_mtime, reverse=True)
                    abort = json.loads(aborts[0].read_text(encoding="utf-8")) if aborts else {}
                    memory = abort.get("memory", {})
                    required = {"physical_available_bytes": int(self.config["resources"]["worker_floor_ram_gib"] * 1024 ** 3),
                                "commit_available_bytes": int(self.config["resources"]["worker_floor_commit_gib"] * 1024 ** 3)}
                    details = resource_details("worker_reserve", memory, required, phase="warm_synthesis")
                    self._stop_owned_process()
                    return {"status": "blocked_memory", "resource_block": details, "retryable": True,
                            "worker_exited_at_reserve": True,
                            "error": "Only the owned voice worker exited at its unchanged safety reserve.",
                            "memory_abort_path": str(aborts[0]) if aborts else None}
                self._stop_owned_process()
                raise RuntimeError(f"The owned TTS worker exited; inspect its worker_log. Exit code: {process.poll() if process else None}")

    def _stop_owned_process(self):
        process = self._process
        worker_handle = None
        if os.name == "nt":
            with self._state_lock:
                worker_pid = self._state.get("worker_pid")
            if worker_pid:
                # Query/synchronize only. The Job Object is the sole mechanism
                # used to terminate its verified owned child tree.
                worker_handle = ctypes.windll.kernel32.OpenProcess(0x101000, False, int(worker_pid))
        if self._job:
            self._job.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        if worker_handle:
            try:
                wait = ctypes.windll.kernel32.WaitForSingleObject
                wait.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
                if wait(worker_handle, 3000) != 0:
                    raise RuntimeError("The owned worker has not finished exiting; do not start a second model.")
            finally:
                ctypes.windll.kernel32.CloseHandle(worker_handle)
        with self._state_lock:
            self._state.update(resident=False, backend_stage="not_loaded", resource_wait=None,
                               last_worker_pid=self._state.get("worker_pid"), worker_pid=None, launcher_pid=None)
        if process is not None and process.poll() is not None:
            for stream in (process.stdin, process.stdout):
                if stream:
                    stream.close()

    def close(self):
        self._closing = True
        self._send({"op": "shutdown"})
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._stop_owned_process()
        if self._log:
            self._log.close()
        if self._job:
            self._job.close()
