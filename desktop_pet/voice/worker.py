"""Owned Irodori process: JSON-lines control in, protocol out, logs on stderr.

No remote service, training, package installation, or other process management
is performed. The facade launches this once using the existing GPU virtualenv.
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import faulthandler
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from collections import OrderedDict
from contextlib import redirect_stdout, nullcontext
from pathlib import Path

from .resources import (GIB, ResourceBlocked, release_cold_stage_memory, resource_details,
                        wait_for_resources)
from .service import ROOT, load_config
from .text import normalize_for_speech
from .idle import IdlePressurePolicy

_PROTOCOL = sys.stdout
_PROTOCOL_LOCK = threading.Lock()


def emit(value: dict):
    with _PROTOCOL_LOCK:
        _PROTOCOL.write(json.dumps(value, ensure_ascii=False) + "\n")
        _PROTOCOL.flush()


class ComputeCancelled(RuntimeError):
    pass


class NeedsSegmentation(ValueError):
    pass


class Control:
    def __init__(self):
        self.commands: queue.Queue = queue.Queue(maxsize=2)
        self.shutdown = threading.Event()
        self.lock = threading.Lock()
        self.cancelled: OrderedDict[str, None] = OrderedDict()
        self.request_id: str | None = None
        self.deadline: float | None = None

    def read(self):
        try:
            for line in self._lines():
                message = json.loads(line)
                if message.get("op") == "cancel":
                    with self.lock:
                        self.cancelled[str(message.get("request_id"))] = None
                        while len(self.cancelled) > 128:
                            self.cancelled.popitem(last=False)
                elif message.get("op") == "shutdown":
                    self.shutdown.set()
                    return
                elif message.get("op") == "synthesize":
                    self.commands.put_nowait(message["request"])
        except Exception:
            traceback.print_exc(file=sys.stderr)
        finally:
            self.shutdown.set()

    def _lines(self):
        if os.name != "nt":
            yield from sys.stdin
            return
        # A blocking background read on piped Windows stdin can deadlock the
        # NumPy/SciPy libgfortran DLL initializer (numpy/numpy#24290). Peek first
        # and read only bytes already present; a partial JSON line stays in our
        # own buffer. Thus lazy native imports and cancellation remain safe.
        import msvcrt
        from ctypes import wintypes
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(sys.stdin.fileno()))
        peek = ctypes.windll.kernel32.PeekNamedPipe
        peek.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        buffer = bytearray()
        while not self.shutdown.is_set():
            available = wintypes.DWORD()
            if not peek(handle, None, 0, None, ctypes.byref(available), None):
                return
            if not available.value:
                self.shutdown.wait(0.02)
                continue
            piece = os.read(sys.stdin.fileno(), min(available.value, 16384))
            if not piece:
                return
            buffer.extend(piece)
            if len(buffer) > 32768:
                raise ValueError("Voice control message exceeds its bound.")
            while b"\n" in buffer:
                line, _, tail = buffer.partition(b"\n")
                buffer = bytearray(tail)
                yield line.decode("utf-8")

    def check(self):
        with self.lock:
            cancelled = self.request_id in self.cancelled
        if self.shutdown.is_set() or cancelled or (self.deadline and time.monotonic() > self.deadline):
            raise ComputeCancelled("Compute cancelled at a safe model/stage boundary.")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for piece in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(piece)
    return digest.hexdigest()


class LocalRuntime:
    def __init__(self, config: dict, control: Control, guard):
        self.config, self.control, self.guard = config, control, guard
        self.runtime = None
        self.load_timings: dict = {}
        self.contracts: dict = {}
        self.requests = 0
        self.reference_hits = 0
        self.token_hits = 0
        self.watermark_settings: dict = {}
        self._restorers = []
        self.preflight_memory = None
        self.memory_releases = []
        self._imports_ready = False
        self.voice_state = "not_loaded"
        self.last_cache_reclaim = None
        self.resource_unload = None
        self.idle_unload_required = False
        self.resource_grace = None
        self._idle_policy = None
        self._first_conditioning_seen = False
        self._first_duration_seen = False

    def health(self):
        return {"resident": self.runtime is not None, "backend_stage": "ready" if self.runtime else "not_loaded",
                "voice_state": self.voice_state, "cache_reclaim": self.last_cache_reclaim,
                "resource_unload": self.resource_unload,
                "resource_grace": self.resource_grace,
                "runtime_load_count": 1 if self.runtime else 0, "completed_compute_requests": self.requests,
                "reference_cache_hits": self.reference_hits, "token_cache_hits": self.token_hits,
                "adapter_path": self.config["adapter_path"], "adapter_sha256": self.config["adapter_sha256"],
                "watermark_backend": "official SilentCipher CPU FP32", "native_acoustic_streaming": False}

    def _maintenance_state(self, value):
        self.voice_state = value["voice_state"]
        if value.get("cache_reclaim") is not None:
            self.last_cache_reclaim = value["cache_reclaim"]
        self.resource_unload = value.get("resource_unload")
        if "resource_grace" in value:
            self.resource_grace = value["resource_grace"]
        event = "resource_unloaded" if self.voice_state == "unloaded_for_resources" else "maintenance"
        emit({"event": event, "health": {**value, "resident": self.runtime is not None and self.voice_state != "unloaded_for_resources"}})

    def _reclaim_unused_cache(self):
        import torch
        runtime = self.runtime
        # Hold strong references and compare real CUDA addresses. empty_cache
        # may return allocator blocks, never these live model/reference tensors.
        live = tuple(runtime.model.parameters()) + tuple(runtime.codec.model.parameters())
        for pair in getattr(self, "_fixed_reference_cache", {}).values():
            live += tuple(value for value in pair if value is not None)
        pointers = tuple(value.data_ptr() for value in live)
        identity = (id(runtime), id(runtime.model), id(runtime.codec.model), tuple(runtime._lora_adapter_names.items()))
        collected = gc.collect()
        torch.cuda.empty_cache()
        heapmin = None
        if os.name == "nt":
            heapmin = int(ctypes.CDLL("ucrtbase")._heapmin())
        preserved = self.runtime is runtime and identity == (id(runtime), id(runtime.model), id(runtime.codec.model), tuple(runtime._lora_adapter_names.items()))
        preserved = preserved and pointers == tuple(value.data_ptr() for value in live)
        if not preserved:
            raise RuntimeError("Cache maintenance changed live model/reference tensor identity.")
        return {"collected_objects": collected, "heapmin_return": heapmin,
                "live_tensor_count": len(live), "live_tensor_data_pointers_unchanged": True}

    def maintain_idle(self, *, trigger):
        if self.runtime is None or self.control.shutdown.is_set():
            return False
        from runtime.irodori_lowmem import memory_status, write_json_new
        if self._idle_policy is None:
            self._idle_policy = IdlePressurePolicy(self.config, snapshot=memory_status, reclaim=self._reclaim_unused_cache,
                                                   notify=self._maintenance_state, closing=self.control.shutdown.is_set)
        policy = self._idle_policy
        try:
            result = policy.check(trigger=trigger)
        except Exception as exc:
            from .resources import memory_status as host_snapshot
            observed = host_snapshot()
            limits = self.config["resources"]
            unload = resource_details("idle_cache_reclaim", observed,
                {"physical_available_bytes": int(limits["idle_keep_free_ram_gib"] * GIB),
                 "commit_available_bytes": int(limits["idle_keep_free_commit_gib"] * GIB)}, phase="idle")
            unload.update(reason="idle_cache_reclaim_failed", error=f"{type(exc).__name__}: {exc}", retryable=True)
            self._maintenance_state({"voice_state": "unloaded_for_resources", "backend_stage": "unloaded_for_resources",
                                     "resident": False, "resource_unload": unload})
            result = {"action": "unload", "resource_unload": unload}
        if result.get("cache_reclaim") or result.get("resource_unload"):
            record = {"schema": "desktop-pet-idle-cache-reclaim-v1", "worker_pid": os.getpid(),
                      "created_utc_seconds": time.time(), **result}
            path = Path(self.config["output_dir"]) / "maintenance" / f"cache-reclaim-{os.getpid()}-{time.time_ns()}.json"
            write_json_new(path, record)
        self.idle_unload_required = result["action"] == "unload"
        return self.idle_unload_required

    def prepare(self):
        if self.runtime is not None:
            return False
        start = time.perf_counter()
        if not self._imports_ready:
            self._stage("imports", release=False)
        emit({"event": "stage", "health": {"backend_stage": "loading"}})
        self.preflight_memory = self.guard.mark("before_torch_import")
        faulthandler.dump_traceback_later(45, file=sys.stderr)
        import torch
        faulthandler.cancel_dump_traceback_later()
        self._imports_ready = True
        self.guard.mark("after_torch_import")
        from runtime import irodori_lowmem as low
        for path in (low.MODEL, low.CODEC, Path(self.config["reference_latent"]),
                     Path(self.config["adapter_path"]) / "adapter_model.safetensors"):
            if not path.is_file():
                raise FileNotFoundError(path)
        if low.MODEL.stat().st_size != 3_064_295_596:
            raise RuntimeError("Pinned base model size mismatch.")
        if _hash(Path(self.config["adapter_path"]) / "adapter_model.safetensors") != self.config["adapter_sha256"]:
            raise RuntimeError("The configured final-step-500 adapter hash does not match.")
        if _hash(Path(self.config["reference_latent"])) != self.config["reference_sha256"]:
            raise RuntimeError("The pinned reference latent hash does not match.")
        revision = subprocess.run(["git", "-C", str(low.VENDOR), "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
        if revision != self.config["upstream_commit"]:
            raise RuntimeError(f"Pinned Irodori upstream commit differs: {revision}")
        self._stage("cuda_init")
        if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
            raise RuntimeError("Native CUDA BF16 is required by this local backend.")
        torch.set_num_threads(int(self.config["cpu_threads"]))
        torch.cuda.init()
        total = torch.cuda.get_device_properties(0).total_memory
        torch.cuda.set_per_process_memory_fraction(min(1.0, self.config["resources"]["allocator_cap_gib"] * GIB / total))
        torch.cuda.reset_peak_memory_stats()
        self.runtime = low.create_runtime(self.guard, self.contracts, self.load_timings, defer_watermark=False,
                                          tensor_chunk_bytes=int(self.config["checkpoint_transfer_chunk_mib"]) * 1024 ** 2,
                                          share_tokenizer_backend=bool(self.config["resources"]["share_tokenizer_backend"]),
                                          decode_only_codec=bool(self.config["resources"]["decode_only_codec"]),
                                          stage_hook=self._stage)
        self._install_hooks()
        self._release_cold("runtime_ready")
        self.load_timings["cold_load_total"] = time.perf_counter() - start
        self.voice_state = "ready"
        emit({"event": "ready", "health": {**self.health(), "load_timings_seconds": self.load_timings}})
        return True

    def _release_cold(self, stage):
        if not self.config["resources"].get("trim_cold_working_set"):
            return
        from runtime.irodori_lowmem import memory_status
        release = release_cold_stage_memory(snapshot=memory_status)
        release["stage"] = stage
        self.memory_releases.append(release)
        print(json.dumps({"cold_temporary_memory_released": release}), file=sys.stderr, flush=True)

    def _stage(self, stage, *, phase="cold_load", release=True):
        self.guard.phase = phase
        self.control.check()
        if release:
            self._release_cold(stage)
        limits = self.config["resources"]
        required = {
            "physical_available_bytes": int((limits["staged_loading_budget_gib"]["host_reserve"] + limits["stage_loading_budget_gib"].get(stage, 0.0)) * GIB),
            "commit_available_bytes": int((limits["worker_floor_commit_gib"] + limits["stage_commit_budget_gib"].get(stage, 0.0)) * GIB),
        }
        def progress(details):
            emit({"event": "stage", "health": {
                "backend_stage": "waiting_resources" if details else stage,
                "resource_wait": {**details, "request_id": self.control.request_id} if details else None,
                "resource_block": None}})
        def cancelled():
            self.control.check()
            return False
        wait_for_resources(lambda: self.guard.check(f"before_{stage}"), required, stage=stage, phase=phase,
                           timeout_seconds=float(limits["stage_wait_seconds"]),
                           cancelled=cancelled, on_wait=progress)
        self.guard.mark(f"stage_{stage}_admitted")

    def _install_hooks(self):
        runtime, control = self.runtime, self.control

        # Hooks belong to this runtime instance only. No vendor source, global
        # torch loading function, sampler, or other process is modified.
        for name in ("encode_conditions", "predict_duration_log_frames", "build_context_kv_cache", "forward_with_encoded_conditions"):
            original = getattr(runtime.model, name)
            def guarded(*args, _original=original, _name=name, **kwargs):
                control.check()
                self.guard.check("model_call")
                first = (_name == "encode_conditions" and not self._first_conditioning_seen) or (_name == "predict_duration_log_frames" and not self._first_duration_seen)
                if first:
                    self.guard.mark(f"before_first_{_name}")
                result = _original(*args, **kwargs)
                if first:
                    self.guard.mark(f"after_first_{_name}")
                    if _name == "encode_conditions":
                        self._first_conditioning_seen = True
                    else:
                        self._first_duration_seen = True
                return result
            setattr(runtime.model, name, guarded)
            self._restorers.append((runtime.model, name, original))

        original_adapter = runtime._prepare_lora_for_request_inner
        def prepare_adapter(adapter_path, *, messages, log_fn):
            resolved = runtime._resolve_lora_adapter_path(adapter_path)
            new = resolved is not None and resolved not in runtime._lora_adapter_names
            load_before = None
            if new:
                from runtime.irodori_lowmem import memory_status
                load_before = memory_status()
            mode = self.config["resources"].get("adapter_loading_mode", "peft_lowmem")
            if mode == "frozen_unmerged_final500":
                if resolved != self.config["adapter_path"]:
                    raise ValueError("The frozen inference model cannot disable or switch its pinned final500 adapter.")
                if new:
                    from .frozen_lora import load_fixed_unmerged
                    from runtime.irodori_lowmem import MODEL
                    self.contracts["frozen_adapter"] = load_fixed_unmerged(
                        runtime.model, resolved, base_checkpoint=MODEL, guard=self.guard,
                        chunk_bytes=int(self.config["checkpoint_transfer_chunk_mib"]) * 1024 ** 2)
                    runtime._lora_adapter_names[resolved] = runtime._adapter_name_for_path(resolved)
                    message = f"info: loaded fixed unmerged LoRA adapter without PEFT: {resolved}"
                else:
                    message = f"info: using cached LoRA adapter: {resolved} (fixed unmerged)"
                messages.append(message)
                log_fn(message)
                context = nullcontext()
            elif mode != "peft_lowmem":
                raise ValueError(f"Unsupported local adapter loading mode: {mode}")
            elif new and self.config["resources"].get("lowmem_peft_inference"):
                from runtime.irodori_lowmem import inference_lora_loader
                with inference_lora_loader():
                    context = original_adapter(adapter_path, messages=messages, log_fn=log_fn)
            else:
                context = original_adapter(adapter_path, messages=messages, log_fn=log_fn)
            if new:
                from runtime.irodori_lowmem import memory_status, write_json_new
                before = memory_status()
                reclaimed = self._reclaim_unused_cache() if self.config["resources"].get("reclaim_after_first_adapter") else {}
                after = memory_status()
                report = {"schema": "desktop-pet-adapter-load-memory-v1", "worker_pid": os.getpid(),
                          "scope": "First unmerged adapter load and cache reclaim, before first condition encoder/duration prediction",
                          "before_adapter_load": load_before, "before": before, "after": after, **reclaimed,
                          "low_cpu_mem_usage": bool(self.config["resources"].get("lowmem_peft_inference")) if mode == "peft_lowmem" else None,
                          "autocast_adapter_dtype": not bool(self.config["resources"].get("lowmem_peft_inference")) if mode == "peft_lowmem" else None,
                          "adapter_loading_mode": mode,
                          "model_merged": False,
                          "gpu_cache_released_bytes": before.get("gpu_reserved_bytes", 0) - after.get("gpu_reserved_bytes", 0),
                          "gpu_allocated_change_bytes": after.get("gpu_allocated_bytes", 0) - before.get("gpu_allocated_bytes", 0),
                          "global_commit_change_bytes": after["commit_available_bytes"] - before["commit_available_bytes"],
                          "adapter_load_private_growth_bytes": before["process_private_bytes"] - load_before["process_private_bytes"],
                          "load_and_reclaim_private_growth_bytes": after["process_private_bytes"] - load_before["process_private_bytes"],
                          "load_and_reclaim_global_commit_change_bytes": after["commit_available_bytes"] - load_before["commit_available_bytes"]}
                self.contracts["adapter_load_memory"] = report
                write_json_new(Path(self.config["output_dir"]) / "maintenance" / f"adapter-load-{os.getpid()}-{time.time_ns()}.json", report)
                self.guard.mark("adapter_loaded_after_cache_reclaim")
            return context
        runtime._prepare_lora_for_request_inner = prepare_adapter

        original_reference = runtime._load_reference_latent
        cached_reference = {}
        self._fixed_reference_cache = cached_reference
        def reference(*, req, batch_size, messages):
            control.check()
            key = (req.ref_latent, req.max_ref_seconds, batch_size, req.no_ref)
            if req.ref_latent == self.config["reference_latent"] and key in cached_reference:
                self.reference_hits += 1
                messages.append("info: reused resident fixed reference latent.")
                return cached_reference[key]
            result = original_reference(req=req, batch_size=batch_size, messages=messages)
            if req.ref_latent == self.config["reference_latent"]:
                cached_reference.clear()
                cached_reference[key] = result
            return result
        runtime._load_reference_latent = reference

        original_conditions = runtime._prepare_request_conditions
        def conditions(plan, *, log_fn):
            control.check()
            result = original_conditions(plan, log_fn=log_fn)
            if plan.target_samples / runtime.codec.sample_rate > float(self.config["max_audio_seconds"]):
                raise NeedsSegmentation("Predicted sentence audio exceeds the local limit; split the text. No speech was truncated.")
            return result
        runtime._prepare_request_conditions = conditions

        original_decode = runtime.codec.decode_latent
        def decode(latent):
            if self.requests == 0:
                self._stage("decode", phase="warm_synthesis")
            if latent.shape[-1] != runtime.codec.latent_dim:
                raise ValueError("Generated latent width does not match the verified codec bottleneck.")
            output = original_decode(latent)
            import torch
            if output.ndim != 3 or output.shape[1] != 1 or output.dtype not in {runtime.codec.dtype, torch.float32}:
                raise ValueError("Decode output rank/channels/dtype differs from the pinned codec contract.")
            self.contracts["decoder_output"] = {"latent_dim": int(latent.shape[-1]), "dtype": str(output.dtype),
                                                "configured_dtype": str(runtime.codec.dtype), "channels": int(output.shape[1]),
                                                "sample_rate": int(runtime.codec.sample_rate)}
            return output
        runtime.codec.decode_latent = decode

        for tokenizer in (runtime.tokenizer, runtime.caption_tokenizer):
            if tokenizer is None:
                continue
            for name in ("encode", "batch_encode"):
                original = getattr(tokenizer, name)
                cache = OrderedDict()
                def tokenized(*args, _original=original, _cache=cache, **kwargs):
                    key_args = tuple(tuple(v) if isinstance(v, list) else v for v in args)
                    key = (key_args, tuple(sorted(kwargs.items())))
                    if key in _cache:
                        self.token_hits += 1
                        _cache.move_to_end(key)
                    else:
                        _cache[key] = _original(*args, **kwargs)
                        if len(_cache) > int(self.config["token_cache_entries"]):
                            _cache.popitem(last=False)
                    value = _cache[key]
                    return tuple(v.clone() for v in value) if isinstance(value, tuple) else value.clone()
                setattr(tokenizer, name, tokenized)

        original_watermark = runtime.watermarker.encode_one
        def encode_one(*args, **kwargs):
            control.check()
            return original_watermark(*args, **kwargs)
        runtime.watermarker.encode_one = encode_one
        def encode_batch(audios, *, sample_rate):
            control.check()
            if not runtime.watermarker.ready:
                raise RuntimeError("Official SilentCipher is unavailable; refusing unwatermarked output.")
            if self.requests == 0:
                self._stage("cpu_watermark", phase="warm_synthesis")
            from runtime.irodori_lowmem import bounded_cpu_watermark
            output, self.watermark_settings = bounded_cpu_watermark(
                runtime.watermarker, audios, sample_rate=sample_rate, guard=self.guard,
                max_chunk_seconds=self.config["watermark_max_chunk_seconds"])
            control.check()
            return output
        runtime.watermarker.encode_batch = encode_batch

    def synthesize(self, request: dict) -> dict:
        started = time.perf_counter()
        self.control.request_id = request["request_id"]
        self.control.deadline = time.monotonic() + float(self.config["request_timeout_seconds"])
        cold = self.prepare()
        import torch
        import numpy as np
        import soundfile as sf
        from irodori_tts.inference_runtime import SamplingRequest, normalize_text
        self.control.check()
        runtime = self.runtime
        self.guard.check("before_request", gpu_reserve=int(self.config["resources"]["warm_min_free_gpu_gib"] * GIB))
        text = normalize_for_speech(request["text_ja"], self.config.get("pronunciation_overrides"))
        text_count = len(runtime.tokenizer.encode(normalize_text(text).strip()))
        caption_count = len(runtime.caption_tokenizer.encode(normalize_text(request["caption_ja"]).strip())) if runtime.caption_tokenizer else 0
        if text_count > runtime.default_text_max_len or caption_count > runtime.default_caption_max_len:
            raise NeedsSegmentation("Text/caption token limit exceeded; refusing silent truncation.")
        token_contract = {}
        for label, wrapper, value in (("text", runtime.tokenizer, text),
                                      ("caption", runtime.caption_tokenizer, request["caption_ja"])):
            if wrapper is None:
                continue
            value = normalize_text(value).strip()
            source = wrapper.rare_token_splitter
            expected = source.encode(value) if source is not None else wrapper.tokenizer.encode(value, add_special_tokens=False)
            expected = ([wrapper.bos_token_id] if wrapper.add_bos else []) + list(expected)
            actual = wrapper.encode(value).tolist()
            if actual != expected:
                raise ValueError(f"Shared {label} tokenizer changed raw token IDs or BOS behavior.")
            token_contract[label] = {"raw_ids_and_bos_exact": True, "bos_enabled": wrapper.add_bos,
                                     "rare_split_enabled": source is not None, "token_count": len(actual),
                                     "ids_sha256": hashlib.sha256(json.dumps(actual).encode("ascii")).hexdigest()}
        self.contracts["request_tokenizer_ids"] = token_contract
        profile = request.get("synthesis_profile", self.config["default_profile"])
        profile_config = self.config["profiles"][profile]
        settings = {**self.config["sampling"], **{key: profile_config[key] for key in
                    ("num_steps", "t_schedule_mode", "sway_coeff")}}
        sampling = SamplingRequest(text=text, caption=request["caption_ja"],
                                   ref_latent=self.config["reference_latent"],
                                   lora_adapter=self.config["adapter_path"], num_candidates=1,
                                   context_kv_cache=True, **settings)
        if self.config["adapter_path"] not in runtime._lora_adapter_names:
            self._stage("adapter", phase="warm_synthesis")
        messages = []
        def log(message):
            self.control.check()
            self.guard.check("synthesis_stage")
            messages.append(message)
            print(message, file=sys.stderr, flush=True)
            emit({"event": "stage", "health": {"backend_stage": message[:200]}})
        stage = time.perf_counter()
        result = runtime.synthesize(sampling, log_fn=log)
        torch.cuda.synchronize()
        synthesize_seconds = time.perf_counter() - stage
        self.control.check()
        audio = result.audio.detach().float().cpu().numpy().reshape(-1)
        if int(result.sample_rate) != 48000 or not len(audio) or not np.isfinite(audio).all() or float(np.abs(audio).max()) <= 1e-7:
            raise RuntimeError("Generated audio is empty, nonfinite, silent, or has an unexpected sample rate.")
        audio_dir = Path(self.config["output_dir"]) / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        wav = audio_dir / f"{request['request_id']}.wav"
        part = wav.with_suffix(".wav.part")
        with part.open("xb") as stream:
            sf.write(stream, audio, 48000, format="WAV", subtype="PCM_16")
        self.control.check()
        part.replace(wav)
        self.requests += 1
        self.guard.mark("request_complete", request_id=request["request_id"])
        stages = dict(result.stage_timings)
        elapsed = time.perf_counter() - started
        response = {
            "status": "generated", "wav_path": str(wav), "watermark_complete": True,
            "backend": self.config["backend"], "worker_pid": os.getpid(),
            "adapter_path": self.config["adapter_path"], "adapter_sha256": self.config["adapter_sha256"],
            "model_path": self.config["model_path"],
            "upstream_commit": self.config["upstream_commit"], "text_spoken_ja": text,
            "synthesis_profile": profile, "profile_experimental": profile_config["experimental"],
            "token_counts": {"text": text_count, "caption": caption_count},
            "timings_seconds": {"cold_request": cold, "cold_load_this_request": self.load_timings["cold_load_total"] if cold else 0.0,
                                "synthesize": synthesize_seconds, "cpu_watermark": stages.get("silentcipher_watermark"),
                                "worker_first_playable": elapsed, "worker_full": elapsed,
                                "upstream_stages": stages},
            "timing_scope": "Complete sentence only: first playable follows model sampling, codec decode, official CPU watermark, and WAV save. Facade records queue/cold process startup and delivery separately. No playback-device latency is measured.",
            "load_timings_seconds": dict(self.load_timings), "sampling": dict(settings),
            "watermark_processing": dict(self.watermark_settings),
            "watermark_model_path": str(Path(runtime.watermarker.model.config.load_ckpt).resolve()),
            "watermark_dtype": str(next(runtime.watermarker.model.enc_c.parameters()).dtype),
            "audio": {"sample_rate": 48000, "channels": 1, "frames": len(audio), "seconds": len(audio) / 48000,
                      "sha256": _hash(wav), "rms": float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))},
            "cache": self.health(), "memory": self.guard.check("after_save"),
            "preflight_memory": self.preflight_memory,
            "lowest_host_headroom_bytes": dict(self.guard.lowest),
            "cold_memory_releases": list(self.memory_releases),
            "loader_contracts": self.contracts,
            "rtf": synthesize_seconds / (len(audio) / 48000), "runtime_messages": messages,
        }
        emit({"event": "session", "health": self.health()})
        return response

    def prewarm(self, request: dict) -> dict:
        started = time.perf_counter()
        self.control.request_id = request["request_id"]
        self.control.deadline = time.monotonic() + float(self.config["request_timeout_seconds"])
        cold = self.prepare()
        self.control.check()
        from irodori_tts.inference_runtime import SamplingRequest
        timings = []
        messages = []
        if self.config["adapter_path"] not in self.runtime._lora_adapter_names:
            self._stage("adapter", phase="cold_load")
        with self.runtime._infer_lock, self.runtime._prepare_lora_for_request(
            self.config["adapter_path"], messages=messages, stage_timings=timings, log_fn=lambda message: None
        ):
            self.runtime._load_reference_latent(
                req=SamplingRequest(text="prewarm", ref_latent=self.config["reference_latent"]),
                batch_size=1, messages=messages)
        self.control.check()
        self.guard.mark("prewarm_complete")
        elapsed = time.perf_counter() - started
        emit({"event": "session", "health": self.health()})
        return {"status": "ready", "worker_pid": os.getpid(), "backend": self.config["backend"],
                "adapter_path": self.config["adapter_path"], "adapter_sha256": self.config["adapter_sha256"],
                "watermark_backend_ready": bool(self.runtime.watermarker.ready), "audio_generated": False,
                "watermark_model_path": str(Path(self.runtime.watermarker.model.config.load_ckpt).resolve()),
                "kernels_warmed": False, "cache": self.health(),
                "load_timings_seconds": dict(self.load_timings),
                "cold_memory_releases": list(self.memory_releases), "loader_contracts": self.contracts,
                "timings_seconds": {"cold_load_this_request": self.load_timings["cold_load_total"] if cold else 0.0,
                                    "prewarm_full": elapsed, "prepare_lora": dict(timings).get("prepare_lora", 0.0)},
                "timing_scope": "Load and cache preparation only. No audio is synthesized and CUDA shape-specific kernels are not warmed."}

    def close(self):
        for owner, name, original in self._restorers:
            setattr(owner, name, original)
        self._restorers.clear()
        getattr(self, "_fixed_reference_cache", {}).clear()
        self.runtime = None
        self.voice_state = "closed"
        gc.collect()
        import torch
        if torch.cuda.is_initialized():
            torch.cuda.empty_cache()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    if Path(sys.executable).resolve() != Path(config["python"]).resolve():
        emit({"event": "fatal", "error": "Run the owned worker using the configured existing GPU virtualenv."})
        return 2
    emit({"event": "boot", "worker_pid": os.getpid()})
    os.environ.update(HF_HOME=config["hf_home"], HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      TOKENIZERS_PARALLELISM="false", IRODORI_SDPA_CUDNN="0")
    sys.path.insert(0, config["upstream_dir"])
    control = Control()
    threading.Thread(target=control.read, name="voice-control", daemon=True).start()
    # A second facade must not accidentally create a second CUDA model.
    mutex = None
    if os.name == "nt":
        ctypes.windll.kernel32.CreateMutexW.restype = ctypes.c_void_p
        mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\NinoDesktopPetIrodoriVoiceWorkerV1")
        if not mutex or ctypes.windll.kernel32.GetLastError() == 183:
            emit({"event": "fatal", "error": "Another desktop pet voice worker already owns the local model. Reuse its service."})
            return 3
    # The existing watchdog may exit only this owned process. Never put it in
    # the UI host: its final low-memory escape is intentionally process-local.
    from runtime import irodori_lowmem as low
    low.configure_paths(config)
    class Guard(low.MemoryGuard):
        def _watch(self):
            while not self.stop.wait(0.25):
                try:
                    self.check("watchdog")
                except MemoryError as exc:
                    memory = low.memory_status()
                    idle = getattr(self, "activity", "initializing") == "idle"
                    code = 72 if idle else 71
                    if idle:
                        details = getattr(exc, "resource_block", {})
                        details.update(phase="idle", stage="idle_hard_reserve", reason="idle_hard_reserve", retryable=True)
                        emit({"event": "resource_unloaded", "health": {
                            "resident": False, "voice_state": "unloaded_for_resources",
                            "backend_stage": "unloaded_for_resources", "resource_unload": details}})
                    try:
                        low.write_json_new(self.directory / f"memory-abort-{self.stamp}.json",
                            {"status": "unloaded_idle_headroom" if idle else "aborted_host_headroom",
                             "error": str(exc), "memory": memory, "snapshots": self.snapshots,
                             "exit_code": code, "scope": "Only the owned voice process exits."})
                    finally:
                        os._exit(code)

        def check(self, stage: str, *, gpu_reserve: int = 0) -> dict:
            memory = low.memory_status()
            for key in self.lowest:
                self.lowest[key] = min(self.lowest[key], memory[key])
            limits = config["resources"]
            if memory["physical_available_bytes"] < limits["worker_floor_ram_gib"] * GIB or memory["commit_available_bytes"] < limits["worker_floor_commit_gib"] * GIB:
                error = MemoryError(f"Owned voice worker stopped at the host reserve during {stage}: {memory}")
                error.resource_block = resource_details(stage, memory,
                    {"physical_available_bytes": int(limits["worker_floor_ram_gib"] * GIB),
                     "commit_available_bytes": int(limits["worker_floor_commit_gib"] * GIB)},
                    phase=getattr(self, "phase", "cold_load"))
                raise error
            if gpu_reserve and memory.get("gpu_free_bytes", gpu_reserve) < gpu_reserve:
                error = MemoryError(f"GPU reserve reached during {stage}: {memory}")
                error.resource_block = resource_details(stage, memory, {"gpu_free_bytes": gpu_reserve},
                                                       phase=getattr(self, "phase", "warm_synthesis"))
                raise error
            return memory
    stamp = f"{os.getpid()}-{time.time_ns()}"
    with redirect_stdout(sys.stderr), Guard(Path(config["output_dir"]), stamp) as guard:
        local = LocalRuntime(config, control, guard)
        exit_code = 0
        try:
            while not control.shutdown.is_set():
                guard.activity = "idle"
                try:
                    request = control.commands.get(timeout=float(config["resources"]["idle_pressure_poll_seconds"]))
                except queue.Empty:
                    if local.maintain_idle(trigger="idle_pressure"):
                        exit_code = 72
                        break
                    continue
                guard.activity = "running"
                if local._idle_policy is not None:
                    local._idle_policy.note_request_admitted()
                    local.resource_grace = None
                    emit({"event": "maintenance", "health": {"resource_grace": None}})
                response = {"status": "failed"}
                try:
                    response = local.prewarm(request) if request.get("_op") == "prewarm" else local.synthesize(request)
                except ComputeCancelled as exc:
                    response = {"status": "cancelled", "error": str(exc)}
                except ResourceBlocked as exc:
                    response = {"status": "blocked_memory", "resource_block": exc.details}
                except MemoryError as exc:
                    response = {"status": "blocked_memory", "error": str(exc),
                                "resource_block": getattr(exc, "resource_block", None), "retryable": True}
                except Exception as exc:
                    traceback.print_exc(file=sys.stderr)
                    torch = sys.modules.get("torch")
                    oom_type = vars(torch).get("OutOfMemoryError") if torch is not None else None
                    if oom_type is not None and isinstance(exc, oom_type):
                        memory = low.memory_status()
                        details = resource_details("gpu_allocation", memory,
                            {"gpu_free_bytes": int(config["resources"]["warm_min_free_gpu_gib"] * GIB)}, phase="warm_synthesis")
                        details.update(category="gpu_memory", reason="allocator_limit_or_gpu_exhaustion",
                                       allocator_cap_bytes=int(config["resources"]["allocator_cap_gib"] * GIB))
                        response = {"status": "blocked_memory", "error": str(exc), "resource_block": details, "retryable": True}
                    else:
                        response = {"status": "failed", "error": f"{type(exc).__name__}: {exc}",
                                    "needs_segmentation": isinstance(exc, NeedsSegmentation)}
                finally:
                    if local.runtime is None or response.get("status") not in {"generated", "ready"}:
                        gc.collect()
                        torch = sys.modules.get("torch")
                        if torch is not None and not getattr(torch.__spec__, "_initializing", False) and torch.cuda.is_initialized():
                            torch.cuda.empty_cache()
                        if local._imports_ready:
                            local._release_cold("incomplete_request_cleanup")
                    control.request_id = None
                    control.deadline = None
                response["initialization_complete"] = local.runtime is not None
                emit({"event": "result", "request_id": request["request_id"], "result": response})
                guard.activity = "idle"
                if local.maintain_idle(trigger="post_request_pressure"):
                    exit_code = 72
                    break
        finally:
            local.close()
    if mutex:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
