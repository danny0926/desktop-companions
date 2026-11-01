"""Strict fixed-final500 unmerged inference, with no PEFT runtime import.

Only this workspace's hashed, vanilla rank-16 adapter is supported. The two
linear operations and addition follow PEFT 0.18.1's unmerged inference order.
Training, adapter switching and general PEFT configurations are unsupported.
"""
from __future__ import annotations

import hashlib
import json
import re
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
from .service import load_config
PINNED_DIRECTORY = Path(load_config()["adapter_path"])
WEIGHT_SHA256 = "69ccbe7abf83735725f11bef586902fa25c52397229032102ab2984031ab0235"
CONFIG_SHA256 = "095011a28295f369ae7cbfcc5be12a6d22d9a886b914187deda74b23f43cbc3b"
TARGET_PATTERN = r"^blocks\.\d+\.attention\.(wq|wk|wv|wo|wk_text|wv_text|wk_speaker|wv_speaker|wk_caption|wv_caption|gate)$"
_LINEAR = None


def _hash(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _header(path):
    with Path(path).open("rb") as stream:
        length = struct.unpack("<Q", stream.read(8))[0]
        if not 0 < length < 16 * 1024 ** 2:
            raise ValueError("Invalid fixed adapter safetensors header size.")
        return {key: value for key, value in json.loads(stream.read(length)).items() if key != "__metadata__"}


def fixed_manifest(directory: str | Path, base_checkpoint: str | Path) -> dict:
    directory = Path(directory).resolve()
    if directory != PINNED_DIRECTORY.resolve():
        raise ValueError("Frozen inference supports only the pinned final-step-500 directory.")
    weights = directory / "adapter_model.safetensors"
    config_path = directory / "adapter_config.json"
    if _hash(weights) != WEIGHT_SHA256 or _hash(config_path) != CONFIG_SHA256:
        raise ValueError("Pinned final500 adapter/config hash mismatch; no PEFT fallback is allowed.")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    supported = {"peft_type": "LORA", "r": 16, "lora_alpha": 32, "lora_dropout": 0.0,
                 "bias": "none", "lora_bias": False, "fan_in_fan_out": False, "use_dora": False,
                 "use_rslora": False, "use_qalora": False, "modules_to_save": ["duration_predictor"],
                 "target_modules": TARGET_PATTERN, "rank_pattern": {}, "alpha_pattern": {},
                 "layer_replication": None, "target_parameters": None, "trainable_token_indices": None}
    for key, value in supported.items():
        if config.get(key) != value:
            raise ValueError(f"Unsupported fixed adapter setting: {key}")
    header, base = _header(weights), _header(base_checkpoint)
    pairs, duration = {}, {}
    for key, value in header.items():
        if not key.startswith("base_model.model.") or value["dtype"] != "F32":
            raise ValueError(f"Unsupported fixed adapter tensor: {key}")
        short = key.removeprefix("base_model.model.")
        if short.endswith(".lora_A.weight") or short.endswith(".lora_B.weight"):
            module, part = short.rsplit(".lora_", 1)
            if re.fullmatch(TARGET_PATTERN, module) is None:
                raise ValueError(f"Unallowlisted LoRA module: {module}")
            pairs.setdefault(module, {})[part[0]] = key
        elif short.startswith("duration_predictor."):
            if short not in base or base[short]["shape"] != value["shape"]:
                raise ValueError(f"Duration predictor tensor differs: {short}")
            duration[short] = key
        else:
            raise ValueError(f"Unallowlisted adapter key: {key}")
    for module, pair in pairs.items():
        if set(pair) != {"A", "B"} or module + ".weight" not in base:
            raise ValueError(f"Incomplete fixed LoRA pair: {module}")
        out_features, in_features = base[module + ".weight"]["shape"]
        if header[pair["A"]]["shape"] != [16, in_features] or header[pair["B"]]["shape"] != [out_features, 16]:
            raise ValueError(f"Fixed LoRA shapes differ: {module}")
    if set(duration) != {key for key in base if key.startswith("duration_predictor.")}:
        raise ValueError("Fixed adapter must contain the complete duration predictor.")
    if len(pairs) != 132 or len(duration) != 31 or len(header) != 295:
        raise ValueError("Pinned adapter tensor count differs.")
    return {"pairs": pairs, "duration": duration, "header": header,
            "weights_path": str(weights), "weight_sha256": WEIGHT_SHA256, "config_sha256": CONFIG_SHA256,
            "rank": 16, "scale": 2.0}


def frozen_linear(base_layer, a, b, *, scale: float):
    """Create vanilla unmerged LoRA without importing PEFT or merging weights."""
    global _LINEAR
    if _LINEAR is None:
        import torch

        class FrozenLoraLinear(torch.nn.Module):
            def __init__(self, base_layer, a, b, scale):
                super().__init__()
                if type(base_layer) is not torch.nn.Linear or base_layer.weight.ndim != 2:
                    raise ValueError("Frozen adapter supports plain torch.nn.Linear only.")
                if a.ndim != 2 or b.ndim != 2 or a.shape[1] != base_layer.in_features or b.shape != (base_layer.out_features, a.shape[0]):
                    raise ValueError("Frozen LoRA linear dimensions differ.")
                if a.dtype != base_layer.weight.dtype or b.dtype != a.dtype or a.device != base_layer.weight.device or b.device != a.device:
                    raise ValueError("Frozen LoRA must share the base layer's dtype/device.")
                self.base_layer = base_layer
                self.lora_A = torch.nn.Linear(a.shape[1], a.shape[0], bias=False, device="meta")
                self.lora_B = torch.nn.Linear(b.shape[1], b.shape[0], bias=False, device="meta")
                self.lora_A.weight = torch.nn.Parameter(a, requires_grad=False)
                self.lora_B.weight = torch.nn.Parameter(b, requires_grad=False)
                self.scaling = float(scale)
                self.eval()

            @property
            def weight(self):
                return self.base_layer.weight

            @property
            def bias(self):
                return self.base_layer.bias

            @property
            def in_features(self):
                return self.base_layer.in_features

            @property
            def out_features(self):
                return self.base_layer.out_features

            def forward(self, x):
                result = self.base_layer(x)
                result_dtype = result.dtype
                x = x.to(self.lora_A.weight.dtype)
                result = result + self.lora_B(self.lora_A(x)) * self.scaling
                return result.to(result_dtype)

        _LINEAR = FrozenLoraLinear
    return _LINEAR(base_layer, a, b, scale)


def load_fixed_unmerged(model, directory, *, base_checkpoint, guard, chunk_bytes) -> dict:
    import torch
    from runtime.irodori_lowmem import TensorFileReader, assign_tensor
    manifest = fixed_manifest(directory, base_checkpoint)
    device, dtype = next(model.parameters()).device, next(model.parameters()).dtype
    # Validate the complete contract before changing any module or weight.
    for name, pair in manifest["pairs"].items():
        module = model.get_submodule(name)
        if type(module) is not torch.nn.Linear or list(module.weight.shape) != [manifest["header"][pair["B"]]["shape"][0], manifest["header"][pair["A"]]["shape"][1]]:
            raise ValueError(f"Unsupported actual target layer: {name}")
    duration_shapes = {"duration_predictor." + key: list(value.shape) for key, value in model.duration_predictor.state_dict().items()}
    if set(duration_shapes) != set(manifest["duration"]):
        raise ValueError("Actual duration predictor keys differ from pinned adapter.")
    for name, key in manifest["duration"].items():
        if duration_shapes[name] != manifest["header"][key]["shape"]:
            raise ValueError(f"Actual duration predictor shape differs: {name}")
    with TensorFileReader(Path(manifest["weights_path"]), transfer_device=device, transfer_dtype=dtype,
                          chunk_bytes=chunk_bytes, guard=guard) as reader:
        reader.safetensors_header()
        for name, pair in manifest["pairs"].items():
            original = model.get_submodule(name)
            a, b = reader.safetensors_tensor(pair["A"]), reader.safetensors_tensor(pair["B"])
            replacement = frozen_linear(original, a, b, scale=manifest["scale"])
            parent_name, _, leaf = name.rpartition(".")
            setattr(model.get_submodule(parent_name), leaf, replacement)
            guard.check("fixed_lora_pair")
        for name, key in manifest["duration"].items():
            value = reader.safetensors_tensor(key)
            assign_tensor(model, name, value, device=device, dtype=dtype)
            del value
            guard.check("fixed_duration_tensor")
    model.eval()
    return {"backend": "frozen_unmerged_final500", "model_merged": False, "peft_import_required": False,
            "weight_sha256": WEIGHT_SHA256, "config_sha256": CONFIG_SHA256,
            "linear_pair_count": len(manifest["pairs"]), "duration_tensor_count": len(manifest["duration"]),
            "inactive_duration_predictor_copy": False, "rank": 16, "scale": 2.0,
            "peft_module_loaded_in_process": "peft" in sys.modules,
            "dtype": str(dtype), "forward_contract": "PEFT 0.18.1 vanilla unmerged order; dropout is identity; no weight merge"}
