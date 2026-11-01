#!/usr/bin/env python3
"""Pinned, offline Irodori inference with streamed BF16 CUDA weight loading.

The bounded construction path differs from upstream. Sampling, conditioning,
tokenizers, dynamic PEFT adapters, codec setup and CPU SilentCipher use upstream.
"""
from __future__ import annotations

import argparse
import ctypes
import gc
import hashlib
import inspect
import json
import os
import re
import sys
import struct
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
VENDOR = ROOT / ".vendor" / "irodori-tts"
VENV = ROOT / ".venv-irodori-gpu"
MODEL = ROOT / "artifacts/irodori-base/model.safetensors"
CODEC = ROOT / "artifacts/semantic-codec/weights.pth"
WATERMARK = ROOT / "artifacts/silentcipher"
GIB = 1024**3


def configure_paths(config: dict) -> None:
    """Set paths in this owned worker only; no dependency on another checkout."""
    global VENDOR, VENV, MODEL, CODEC, WATERMARK
    VENDOR = Path(config["upstream_dir"])
    VENV = Path(config["python"]).parent.parent
    MODEL = Path(config["model_path"])
    CODEC = Path(config["codec_path"])
    WATERMARK = Path(config["watermark_dir"])


def memory_status() -> dict:
    """Windows physical/commit and this process only; no other process changes."""
    if os.name != "nt":
        raise RuntimeError("This bounded desktop runner currently requires Windows.")

    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
            (name, ctypes.c_ulonglong) for name in
            ("total_phys", "avail_phys", "total_commit", "avail_commit", "total_virtual", "avail_virtual", "extended")
        ]

    class Process(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in
            ("peak_rss", "rss", "peak_paged", "paged", "peak_nonpaged", "nonpaged", "pagefile", "peak_pagefile", "private")
        ]

    status = Status()
    status.length = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise ctypes.WinError()
    process = Process()
    process.cb = ctypes.sizeof(process)
    ok = ctypes.windll.kernel32.K32GetProcessMemoryInfo(ctypes.c_void_p(-1), ctypes.byref(process), process.cb)
    result = {
        "physical_available_bytes": int(status.avail_phys),
        "commit_available_bytes": int(status.avail_commit),
        "physical_total_bytes": int(status.total_phys),
        "commit_limit_bytes": int(status.total_commit),
    }
    if ok:
        result.update(process_working_set_bytes=int(process.rss), process_peak_working_set_bytes=int(process.peak_rss), process_private_bytes=int(process.private))
    torch = sys.modules.get("torch")
    # Do not touch PyTorch's lazy attributes from the watchdog while the main
    # thread imports torch: that can recursively import CUDA on a second thread
    # and contend on Python's module locks. Host RAM/commit checks still run.
    torch_ready = torch is not None and not getattr(torch.__spec__, "_initializing", False)
    cuda = vars(torch).get("cuda") if torch_ready else None
    if cuda is not None and cuda.is_initialized():
        free, total = cuda.mem_get_info()
        result.update(gpu_free_bytes=int(free), gpu_total_bytes=int(total), gpu_allocated_bytes=cuda.memory_allocated(), gpu_reserved_bytes=cuda.memory_reserved(), gpu_peak_allocated_bytes=cuda.max_memory_allocated(), gpu_peak_reserved_bytes=cuda.max_memory_reserved())
    return result


def write_json_new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


class MemoryGuard:
    """Stop only this inference process before host commit becomes exhausted."""
    def __init__(self, directory: Path, stamp: str):
        self.directory, self.stamp = directory, stamp
        self.stop = threading.Event()
        self.lowest = {"physical_available_bytes": 2**64, "commit_available_bytes": 2**64}
        self.snapshots = []

    def check(self, stage: str, *, gpu_reserve: int = 0) -> dict:
        mem = memory_status()
        for key in self.lowest:
            self.lowest[key] = min(self.lowest[key], mem[key])
        if mem["physical_available_bytes"] < 350 * 1024**2 or mem["commit_available_bytes"] < 768 * 1024**2:
            raise MemoryError(f"Host headroom reserve reached during {stage}: {mem}")
        if gpu_reserve and mem.get("gpu_free_bytes", gpu_reserve) < gpu_reserve:
            raise MemoryError(f"GPU headroom reserve reached during {stage}: {mem}")
        return mem

    def mark(self, stage: str, **extra) -> dict:
        entry = {"stage": stage, "elapsed_seconds": time.perf_counter() - self.started, **self.check(stage), **extra}
        self.snapshots.append(entry)
        print(json.dumps({"memory": entry}, ensure_ascii=False), flush=True)
        return entry

    def __enter__(self):
        self.started = time.perf_counter()
        self.check("before_import")
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()
        return self

    def _watch(self):
        while not self.stop.wait(0.5):
            try:
                self.check("watchdog")
            except MemoryError as exc:
                try:
                    write_json_new(self.directory / f"memory-abort-{self.stamp}.json", {"status": "aborted_host_headroom", "error": str(exc), "memory": memory_status(), "snapshots": self.snapshots, "scope": "Only this inference process exits; user applications and system settings are untouched."})
                finally:
                    os._exit(71)

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=2)


def assign_tensor(module, name: str, tensor, *, device, dtype) -> None:
    """One CPU-mapped source -> final device/dtype, never a full FP32 CUDA model."""
    import torch
    parent_name, _, leaf = name.rpartition(".")
    parent = module.get_submodule(parent_name) if parent_name else module
    target_dtype = dtype if tensor.is_floating_point() else tensor.dtype
    value = tensor.to(device=device, dtype=target_dtype)
    if leaf in parent._parameters:
        parent._parameters[leaf] = torch.nn.Parameter(value, requires_grad=False)
    elif leaf in parent._buffers:
        parent._buffers[leaf] = value
    else:
        raise KeyError(f"Unknown state tensor: {name}")


class TensorFileReader:
    """Read one contiguous tensor into one temporary CPU allocation.

    Windows copy-on-write mappings reserve commit for the entire checkpoint and
    retain touched pages until unmapped. Direct readinto avoids both effects.
    """
    def __init__(self, path: Path, *, transfer_device=None, transfer_dtype=None, chunk_bytes=None, guard=None):
        self.path = path
        self.transfer_device, self.transfer_dtype = transfer_device, transfer_dtype
        self.chunk_bytes, self.guard = chunk_bytes, guard

    def __enter__(self):
        self.stream = self.path.open("rb", buffering=0)
        return self

    def __exit__(self, *args):
        self.stream.close()

    def tensor(self, *, offset: int, shape, dtype):
        import torch
        if self.chunk_bytes is not None:
            if self.transfer_device is None or int(self.chunk_bytes) < 16:
                raise ValueError("Chunked tensor transfer requires a device and at least 16 bytes")
            target_dtype = self.transfer_dtype if dtype.is_floating_point else dtype
            if target_dtype is None:
                target_dtype = dtype
            result = torch.empty(tuple(shape), device=self.transfer_device, dtype=target_dtype)
            destination = result.reshape(-1)
            elements = destination.numel()
            element_size = torch.empty((), dtype=dtype).element_size()
            slab_elements = min(elements, max(1, int(self.chunk_bytes) // element_size))
            slab = torch.empty((slab_elements,), device="cpu", dtype=dtype)
            view = memoryview(slab.view(torch.uint8).numpy()).cast("B")
            self.stream.seek(offset)
            position = 0
            while position < elements:
                count = min(slab_elements, elements - position)
                byte_count, copied = count * element_size, 0
                while copied < byte_count:
                    amount = self.stream.readinto(view[copied:byte_count])
                    if not amount:
                        raise EOFError(f"Incomplete checkpoint tensor at byte {offset}")
                    copied += amount
                destination[position:position + count].copy_(slab[:count])
                position += count
                if self.guard is not None:
                    self.guard.check("checkpoint_slab_transfer")
            return result
        value = torch.empty(tuple(shape), device="cpu", dtype=dtype)
        view = memoryview(value.reshape(-1).view(torch.uint8).numpy()).cast("B")
        self.stream.seek(offset)
        position = 0
        while position < len(view):
            amount = self.stream.readinto(view[position:])
            if not amount:
                raise EOFError(f"Incomplete checkpoint tensor at byte {offset}")
            position += amount
        return value

    def safetensors_header(self):
        self.stream.seek(0)
        raw = self.stream.read(8)
        header_size = struct.unpack("<Q", raw)[0]
        if not 0 < header_size < 16 * 1024**2:
            raise ValueError("Unexpected safetensors header size")
        header = json.loads(self.stream.read(header_size))
        self.data_start = 8 + header_size
        self.header = {k: v for k, v in header.items() if k != "__metadata__"}
        return header.get("__metadata__", {})

    def safetensors_tensor(self, name):
        import torch
        entry = self.header[name]
        dtype = {"F32": torch.float32, "F16": torch.float16, "BF16": torch.bfloat16, "F64": torch.float64, "I64": torch.int64, "I32": torch.int32, "BOOL": torch.bool}[entry["dtype"]]
        offset, end = entry["data_offsets"]
        elements = 1
        for dimension in entry["shape"]:
            elements *= dimension
        if elements * torch.empty((), dtype=dtype).element_size() != end - offset:
            raise ValueError(f"Invalid safetensors offsets: {name}")
        return self.tensor(offset=self.data_start + offset, shape=entry["shape"], dtype=dtype)


def check_and_stream(module, names, get_shape, get_tensor, *, device, dtype, guard=None) -> dict:
    """Validate the complete state contract before materializing any weight."""
    import torch
    expected = module.state_dict()
    actual_names = set(names)
    if set(expected) != actual_names:
        raise ValueError(f"State keys differ: missing={sorted(set(expected)-actual_names)}, unexpected={sorted(actual_names-set(expected))}")
    for name, tensor in expected.items():
        if tuple(tensor.shape) != tuple(get_shape(name)):
            raise ValueError(f"Shape differs for {name}: model={tuple(tensor.shape)}, checkpoint={tuple(get_shape(name))}")
    # Capture identity before replacement. Stream in ordinary state_dict order,
    # so a later alias has the same last-copy-wins semantics as load_state_dict.
    alias_groups = {}
    for name, parameter in module.named_parameters(remove_duplicate=False):
        alias_groups.setdefault(id(parameter), []).append(name)
    aliases = [group for group in alias_groups.values() if len(group) > 1]
    canonical = {name: group[0] for group in aliases for name in group[1:]}
    ordered_names = list(expected)
    count = 0
    total = sum(t.numel() for t in expected.values())
    del expected
    for name in ordered_names:
        value = get_tensor(name)
        if name in canonical:
            parameter = module.get_parameter(canonical[name])
            with torch.no_grad():
                parameter.copy_(value)
            parent_name, _, leaf = name.rpartition(".")
            parent = module.get_submodule(parent_name) if parent_name else module
            parent._parameters[leaf] = parameter
        else:
            assign_tensor(module, name, value, device=device, dtype=dtype)
        del value
        count += 1
        if guard is not None and count % 32 == 0:
            guard.check(f"stream_weight_{count}")
    # Nonpersistent buffers such as ModernBERT frequencies were constructed on
    # CPU (include_buffers=False). Preserve complex RoPE values and integer types.
    for child in module.modules():
        for name, value in list(child._buffers.items()):
            if value is not None:
                if value.is_meta:
                    raise ValueError(f"Unexpected meta buffer: {name}")
                child._buffers[name] = value.to(device=device, dtype=dtype if value.is_floating_point() else value.dtype)
    meta = [n for n, t in list(module.named_parameters()) + list(module.named_buffers()) if t.is_meta]
    if meta:
        raise ValueError(f"Unmaterialized tensors: {meta}")
    complex_buffers = {n: str(t.dtype) for n, t in module.named_buffers() if t.is_complex()}
    for group in aliases:
        if not all(module.get_parameter(n) is module.get_parameter(group[0]) for n in group):
            raise ValueError(f"Parameter alias identity lost: {group}")
    return {"state_keys_exact": True, "state_shapes_exact": True, "tensors_loaded": count, "elements_loaded": total, "meta_tensors_remaining": 0, "parameter_alias_groups_preserved": aliases, "complex_buffers": complex_buffers, "parameter_dtype_counts": {str(d): sum(p.numel() for p in module.parameters() if p.dtype == d) for d in set(p.dtype for p in module.parameters())}}


@contextmanager
def lowmem_codec_loader(*, device, dtype, guard, contracts, tensor_chunk_bytes=None):
    """Use upstream codec setup with metadata-only checkpoint discovery."""
    import torch
    from accelerate import init_empty_weights
    from dacvae import DACVAE
    from torch._subclasses.fake_tensor import FakeTensorMode

    def load(cls, path):
        guard.check("codec_checkpoint")
        with FakeTensorMode():
            payload = torch.load(path, map_location="cpu", weights_only=True, mmap=True)
        metadata = payload["metadata"]
        kwargs = dict(metadata["kwargs"])
        allowed = inspect.signature(cls.__init__).parameters
        kwargs = {k: v for k, v in kwargs.items() if k in allowed}
        with init_empty_weights(include_buffers=False):
            model = cls(**kwargs)
        state = payload["state_dict"]
        for name, value in state.items():
            if not value.is_contiguous() or not hasattr(value.untyped_storage(), "_checkpoint_offset"):
                raise ValueError(f"Codec tensor needs unsupported layout/offset: {name}")
        with TensorFileReader(Path(path), transfer_device=device, transfer_dtype=dtype,
                              chunk_bytes=tensor_chunk_bytes, guard=guard) as reader:
            def read(name):
                value = state[name]
                offset = value.untyped_storage()._checkpoint_offset + value.storage_offset() * value.element_size()
                return reader.tensor(offset=offset, shape=value.shape, dtype=value.dtype)
            contracts["codec"] = check_and_stream(model, list(state), lambda n: state[n].shape, read, device=device, dtype=dtype, guard=guard)
        model.metadata = metadata
        del payload, state
        gc.collect()
        guard.mark("codec_weights_loaded")
        return model.eval()

    with patch.object(DACVAE, "load", classmethod(load)):
        yield


@contextmanager
def inference_lora_loader():
    """Use PEFT's supported inference flags without transient training upcasts.

    The pinned runtime still casts the completed model to BF16 as before. This
    scoped hook changes construction/loading only, not unmerged LoRA forward.
    """
    from peft import PeftModel
    from irodori_tts import inference_runtime as upstream

    def load(model, adapter_path, *, is_trainable, adapter_name="default", torch_device=None):
        if is_trainable:
            raise ValueError("The resident low-memory loader is inference-only.")
        options = {"is_trainable": False, "autocast_adapter_dtype": False,
                   "low_cpu_mem_usage": True, "torch_device": torch_device}
        if isinstance(model, PeftModel):
            if adapter_name not in model.peft_config:
                model.load_adapter(str(adapter_path), adapter_name=adapter_name, **options)
            model.set_adapter(adapter_name)
            return model
        return PeftModel.from_pretrained(model, str(adapter_path), adapter_name=adapter_name, **options)

    with patch.object(upstream, "load_lora_adapter", load):
        yield


def create_decode_only_codec(upstream, *, device, dtype, expected_latent_dim, contracts):
    """Keep the pinned decoder setup without an unused encoder warmup.

    This service uses a preencoded reference. Infer the exact encoded width from
    the loaded bottleneck and cross-check it against the model, rather than
    allocating convolution workspaces for an encoder dummy waveform.
    """
    import torch
    from dacvae import DACVAE
    model = DACVAE.load(str(CODEC)).eval().to(device=device, dtype=dtype)
    dimension = int(model.quantizer.codebook_dim)
    projected = int(model.quantizer.out_proj.in_channels)
    if dimension != int(expected_latent_dim) or projected != dimension:
        raise ValueError(f"Codec bottleneck width differs: {dimension}, {projected}, expected {expected_latent_dim}")
    decoder = getattr(model, "decoder", None)
    if decoder is not None and hasattr(decoder, "alpha"):
        decoder.alpha = 0.0
        if hasattr(decoder, "wm_model"):
            def watermark_passthrough(x, message=None, _decoder=decoder):
                del message
                return _decoder.wm_model.encoder_block.forward_no_conv(x)
            decoder.watermark = watermark_passthrough
    upstream.DACVAECodec._configure_deterministic_decode(model=model, device=device)
    codec = upstream.DACVAECodec(
        model=model, sample_rate=int(model.sample_rate), latent_dim=dimension,
        device=torch.device(device), dtype=next(model.parameters()).dtype,
        deterministic_encode=True, deterministic_decode=True, normalize_db=-16.0)
    def unavailable_encoder(*args, **kwargs):
        raise RuntimeError("This resident codec uses a fixed preencoded reference; live reference encoding is disabled.")
    codec.encode_waveform = unavailable_encoder
    contracts["decode_only_codec"] = {"latent_width_from_bottleneck": dimension,
                                      "latent_width_matches_model": True, "encoder_dummy_forward_executed": False,
                                      "decoder_configuration": "pinned upstream alpha=0 and deterministic message; official SilentCipher remains required"}
    return codec


def create_runtime(guard: MemoryGuard, contracts: dict, timings: dict, *, defer_watermark: bool = False,
                   tensor_chunk_bytes=None, share_tokenizer_backend=False, decode_only_codec=False, stage_hook=None):
    """Pinned private-helper integration; vendor code remains unchanged."""
    if stage_hook is not None:
        stage_hook("model_construct")
    import torch
    from accelerate import init_empty_weights
    from irodori_tts import inference_runtime as upstream

    key = upstream.RuntimeKey(checkpoint=str(MODEL), model_device="cuda", model_precision="bf16", codec_repo=str(CODEC), codec_device="cuda", codec_precision="bf16", compile_model=False)
    device, dtype = torch.device("cuda:0"), torch.bfloat16
    started = time.perf_counter()
    with TensorFileReader(MODEL, transfer_device=device, transfer_dtype=dtype,
                          chunk_bytes=tensor_chunk_bytes, guard=guard) as reader:
        metadata = reader.safetensors_header()
        flat = upstream._parse_json_mapping(metadata.get("config_json"), field="config_json", path=MODEL, required=True)
        model_dict, train = upstream._split_flat_checkpoint_config(MODEL, flat)
        text_config = upstream._parse_json_mapping(metadata.get("text_encoder_config_json"), field="text_encoder_config_json", path=MODEL, required=True)
        cfg = upstream.merge_dataclass_overrides(upstream.ModelConfig(), model_dict, section="checkpoint model_config")
        with init_empty_weights(include_buffers=False):
            model = upstream.TextToLatentRFDiT(cfg, pretrained_backbone_config=text_config, load_pretrained_backbone_weights=False)
        guard.mark("meta_model_constructed")
        if stage_hook is not None:
            stage_hook("model_weights")
        contracts["model"] = check_and_stream(model, list(reader.header), lambda n: reader.header[n]["shape"], reader.safetensors_tensor, device=device, dtype=dtype, guard=guard)
    model.eval()
    torch.cuda.synchronize()
    timings["model_load_seconds"] = time.perf_counter() - started
    gc.collect()
    guard.mark("model_weights_loaded")
    if stage_hook is not None:
        stage_hook("tokenizers")
    started = time.perf_counter()
    source, local = upstream._resolve_tokenizer_source(MODEL, cfg.text_tokenizer_repo)
    if not local:
        raise RuntimeError("Bundled tokenizer required for fully offline inference.")
    tokenizer = upstream.PretrainedTextTokenizer.from_pretrained(source, add_bos=bool(cfg.text_add_bos), local_files_only=True)
    if key.split_rare_tokens and cfg.text_tokenizer_repo == upstream.SPLIT_TOKENIZER_REPO:
        tokenizer.enable_rare_token_split()
    if cfg.use_caption_condition and share_tokenizer_backend:
        # Wrappers retain independent BOS/splitting behavior, while immutable
        # vocabulary/backend storage need not be loaded a second time.
        caption_tokenizer = upstream.PretrainedTextTokenizer(tokenizer.tokenizer, add_bos=cfg.caption_add_bos_resolved)
        contracts["shared_tokenizer_backend"] = {"backing_object_shared": caption_tokenizer.tokenizer is tokenizer.tokenizer,
                                                  "text_and_caption_wrappers_independent": caption_tokenizer is not tokenizer,
                                                  "text_rare_split_preserved": tokenizer.rare_token_splitter is not None,
                                                  "caption_rare_split": caption_tokenizer.rare_token_splitter is not None}
    else:
        caption_tokenizer = upstream.PretrainedTextTokenizer.from_pretrained(source, add_bos=cfg.caption_add_bos_resolved, local_files_only=True) if cfg.use_caption_condition else None
    timings["tokenizer_load_seconds"] = time.perf_counter() - started
    guard.mark("tokenizers_loaded")
    if stage_hook is not None:
        stage_hook("codec_weights")
    started = time.perf_counter()
    with lowmem_codec_loader(device=device, dtype=dtype, guard=guard, contracts=contracts, tensor_chunk_bytes=tensor_chunk_bytes):
        if decode_only_codec:
            codec = create_decode_only_codec(upstream, device=device, dtype=dtype, expected_latent_dim=cfg.latent_dim, contracts=contracts)
        else:
            codec = upstream.DACVAECodec.load(repo_id=str(CODEC), device=str(device), dtype=dtype, deterministic_encode=True, deterministic_decode=True)
    if codec.latent_dim != cfg.latent_dim:
        raise ValueError("Codec/model latent dimension mismatch")
    timings["codec_load_seconds"] = time.perf_counter() - started
    guard.mark("codec_ready")
    if stage_hook is not None:
        stage_hook("watermarker")
    train = train or {}
    text_max = int(train.get("max_text_len", 256))
    started = time.perf_counter()
    watermarker_cls = upstream.SilentCipherWatermarker
    class DeferredWatermarkStage:
        # This hook lets upstream finish decoding. It never produces a completed
        # artifact: the caller saves pending tensors for the official CPU stage.
        ready = True
        model = None
        def encode_batch(self, audios, *, sample_rate):
            return audios
    def pinned_watermarker(**kwargs):
        import silentcipher
        original = silentcipher.get_model
        checkpoint = WATERMARK / "44_1_khz/73999_iteration"
        def get_model(**settings):
            return original(**{**settings, "ckpt_path": str(checkpoint),
                               "config_path": str(checkpoint / "hparams.yaml")})
        with patch.object(silentcipher, "get_model", get_model):
            return watermarker_cls(device="cpu")
    factory = (lambda **kwargs: DeferredWatermarkStage()) if defer_watermark else pinned_watermarker
    with patch.object(upstream, "SilentCipherWatermarker", factory):
        runtime = upstream.InferenceRuntime(key=key, model_cfg=cfg, train_cfg=train, model=model, tokenizer=tokenizer, caption_tokenizer=caption_tokenizer, codec=codec, default_text_max_len=text_max, default_caption_max_len=int(train.get("max_caption_len", text_max)), default_max_ref_seconds=upstream._default_max_ref_seconds(train))
    timings["watermark_runtime_init_seconds"] = time.perf_counter() - started
    if not runtime.watermarker.ready:
        raise RuntimeError("Normal SilentCipher watermark is required; local backend failed to load.")
    guard.mark("runtime_ready", watermark_backend_loaded=not defer_watermark, watermark_deferred=defer_watermark)
    return runtime


def loader_contract() -> dict:
    """Small actual equivalence check for casting, persistent and complex buffers."""
    import torch
    from accelerate import init_empty_weights
    from safetensors.torch import save_file
    from torch._subclasses.fake_tensor import FakeTensorMode

    class Tiny(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.linear = torch.nn.Linear(4, 3)
            self.shared_linear = self.linear
            self.register_buffer("scale", torch.ones(3))
            self.register_buffer("rope", torch.tensor([1 + 2j], dtype=torch.complex64), persistent=False)

        def forward(self, x):
            return self.linear(x) * self.scale

    torch.manual_seed(6)
    normal = Tiny().eval()
    state = {k: v.detach().clone() for k, v in normal.state_dict().items()}
    with init_empty_weights(include_buffers=False):
        streamed = Tiny()
    with tempfile.TemporaryDirectory(prefix="irodori-loader-") as tmp:
        path = Path(tmp) / "tiny.safetensors"
        save_file(state, str(path))
        with TensorFileReader(path) as reader:
            reader.safetensors_header()
            result = check_and_stream(streamed, list(reader.header), lambda n: reader.header[n]["shape"], reader.safetensors_tensor, device="cpu", dtype=torch.float32)
        # The resident desktop service additionally limits a large tensor's
        # CPU staging allocation. Check actual multi-slab values and casts.
        with TensorFileReader(path, transfer_device="cpu", transfer_dtype=torch.bfloat16, chunk_bytes=16) as reader:
            reader.safetensors_header()
            for name, expected in state.items():
                actual = reader.safetensors_tensor(name)
                assert torch.equal(actual, expected.to(dtype=actual.dtype)), f"Chunked tensor differs: {name}"
            result["chunked_transfer_values_and_dtype_exact"] = True
        archive = Path(tmp) / "tiny.pth"
        torch.save({"metadata": {"kwargs": {}}, "state_dict": state}, archive)
        with FakeTensorMode():
            fake_state = torch.load(archive, weights_only=True, mmap=True)["state_dict"]
        with TensorFileReader(archive) as reader:
            for name, value in fake_state.items():
                restored = reader.tensor(offset=value.untyped_storage()._checkpoint_offset, shape=value.shape, dtype=value.dtype)
                assert torch.equal(restored, state[name]), f"PyTorch archive offset differs: {name}"
    x = torch.randn(2, 4)
    assert torch.equal(normal(x), streamed(x)), "Streamed FP32 output differs"
    assert streamed.rope.dtype == torch.complex64 and streamed.rope.imag.item() == 2.0
    assert streamed.linear.weight is streamed.shared_linear.weight
    result.update(fp32_output_exact=True, complex_imaginary_value_preserved=True, parameter_alias_identity_preserved=True, pytorch_archive_direct_offsets_exact=True)
    return result


def bounded_cpu_watermark(watermarker, audios, *, sample_rate, guard, max_chunk_seconds=None):
    """Official payload/model on bounded pieces, blending only their perturbation."""
    import math
    import torch
    available = guard.check("before_bounded_watermark")["physical_available_bytes"]
    target = 1.0 if available < int(1.1 * GIB) else (2.0 if available < int(1.8 * GIB) else 4.0)
    if max_chunk_seconds is not None:
        if not 0.5 <= float(max_chunk_seconds) <= 4.0:
            raise ValueError("Watermark chunk bound must be between 0.5 and 4 seconds")
        target = min(target, float(max_chunk_seconds))
    context = max(1, round(sample_rate * 0.05))
    outputs, counts = [], []
    for audio in audios:
        original_shape = audio.shape
        source = audio.detach().float().cpu().reshape(-1)
        frames = source.numel()
        count = max(1, math.ceil(frames / (sample_rate * target)))
        delta_sum = torch.zeros_like(source)
        coverage = torch.zeros_like(source)
        for index in range(count):
            boundary_start = index * frames // count
            boundary_end = (index + 1) * frames // count
            start, end = max(0, boundary_start - context), min(frames, boundary_end + context)
            piece = source[start:end]
            encoded = watermarker.encode_one(piece, sample_rate=sample_rate).reshape(-1)
            if encoded.numel() != piece.numel():
                raise ValueError(f"Official watermark changed piece length: {encoded.numel()} != {piece.numel()}")
            weights = torch.ones_like(piece)
            fade = min(context * 2, piece.numel())
            if start > 0:
                weights[:fade] = torch.linspace(0.0, 1.0, fade)
            if end < frames:
                weights[-fade:] = torch.linspace(1.0, 0.0, fade)
            delta_sum[start:end].add_((encoded - piece) * weights)
            coverage[start:end].add_(weights)
            guard.mark("cpu_watermark_chunk_complete", chunk=index + 1, chunks=count)
        if not bool((coverage > 0).all()):
            raise ValueError("Watermark chunk coverage is incomplete")
        output = source + delta_sum / coverage
        if output.numel() != frames or not bool(torch.isfinite(output).all()) or float(output.abs().max()) <= 1e-7:
            raise ValueError("Bounded watermark output failed length/finite/nonzero contract")
        outputs.append(output.reshape(original_shape))
        counts.append(count)
    return outputs, {"backend": "official SilentCipher CPU FP32", "payload": "IRDTS", "target_chunk_seconds": target, "context_each_side_seconds": 0.05, "total_overlap_seconds": 0.1, "chunk_counts": counts, "blend": "normalized overlap-add of watermark perturbations; source speech is added once", "full_audio_coverage_verified": True, "frame_count_preserved": True}
