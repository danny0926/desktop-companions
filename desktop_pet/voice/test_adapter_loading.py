"""Actual PEFT tiny-CPU parity; no voice checkpoint or CUDA model is loaded."""
from __future__ import annotations

import json
import sys
from importlib.util import find_spec
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
from .service import load_config
_CONFIG = load_config()
sys.path.insert(0, _CONFIG["upstream_dir"])


@unittest.skipUnless(all(find_spec(name) is not None for name in ("torch", "peft", "irodori_tts", "dacvae", "silentcipher")),
                     "Optional voice environment is not installed")
class AdapterLoadingContracts(unittest.TestCase):
    def test_00_supported_lowmem_flags_preserve_final_bf16_weights_and_forward(self):
        from runtime.irodori_lowmem import memory_status, inference_lora_loader
        import torch
        from irodori_tts import inference_runtime as upstream
        # Match imports already present in the actual runtime before attributing
        # any additional private bytes to PEFT itself.
        from accelerate import init_empty_weights
        from torch._subclasses.fake_tensor import FakeTensorMode
        from dacvae import DACVAE
        from transformers.models.modernbert.modeling_modernbert import ModernBertModel
        import silentcipher
        before = memory_status()
        peft_loaded_before = "peft" in sys.modules
        from peft import LoraConfig, get_peft_model
        after = memory_status()

        class Tiny(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(4, 4)
                self.duration_predictor = torch.nn.Linear(4, 2)

            def forward(self, x):
                return self.duration_predictor(self.linear(x))

        torch.manual_seed(17)
        source = Tiny()
        state = {key: value.clone() for key, value in source.state_dict().items()}
        trained = get_peft_model(source, LoraConfig(r=2, lora_alpha=4, target_modules=["linear"],
                                                  modules_to_save=["duration_predictor"], lora_dropout=0.0))
        with torch.no_grad():
            for name, parameter in trained.named_parameters():
                if "lora_B" in name or "modules_to_save" in name:
                    parameter.copy_(torch.randn_like(parameter) * 0.1)

        def base():
            model = Tiny()
            model.load_state_dict(state)
            return model.to(dtype=torch.bfloat16).eval()

        with tempfile.TemporaryDirectory(prefix="pet-peft-parity-") as temporary:
            trained.save_pretrained(temporary)
            baseline = upstream.load_lora_adapter(base(), temporary, is_trainable=False, torch_device="cpu")
            baseline = upstream._move_inference_module(baseline, device=torch.device("cpu"), dtype=torch.bfloat16).eval()
            with inference_lora_loader():
                optimized = upstream.load_lora_adapter(base(), temporary, is_trainable=False, torch_device="cpu")
            optimized = upstream._move_inference_module(optimized, device=torch.device("cpu"), dtype=torch.bfloat16).eval()
            left, right = baseline.state_dict(), optimized.state_dict()
            self.assertEqual(set(left), set(right))
            for key in left:
                self.assertTrue(torch.equal(left[key], right[key]), key)
            with torch.inference_mode():
                x = torch.randn(3, 4, dtype=torch.bfloat16)
                self.assertTrue(torch.equal(baseline(x), optimized(x)))
            self.assertFalse(torch.cuda.is_initialized())
        report = {"schema": "tiny-peft-cpu-loading-parity-v1", "weights_exact": True, "forward_exact": True,
                  "dtype": "bfloat16", "model_merged": False, "cuda_initialized": False,
                  "peft_import_before": before, "peft_import_after": after,
                  "peft_loaded_before_measurement": peft_loaded_before,
                  "peft_import_private_growth_bytes": after["process_private_bytes"] - before["process_private_bytes"]}
        path = ROOT / "outputs/pet-voice-v1/tiny-peft-loading-parity.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    def test_frozen_unmerged_matches_real_peft_with_active_duration_replacement(self):
        import copy
        import torch
        from peft import LoraConfig, get_peft_model
        from .frozen_lora import frozen_linear
        from runtime.irodori_lowmem import assign_tensor

        class Tiny(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.linear = torch.nn.Linear(8, 6, bias=False)
                self.duration_predictor = torch.nn.Linear(6, 2)

            def forward(self, x):
                return self.duration_predictor(self.linear(x))

        torch.manual_seed(23)
        baseline = get_peft_model(Tiny().to(dtype=torch.bfloat16),
            LoraConfig(r=2, lora_alpha=4, target_modules=["linear"], modules_to_save=["duration_predictor"], lora_dropout=0.0),
            autocast_adapter_dtype=False).eval()
        core = baseline.get_base_model()
        with torch.no_grad():
            core.linear.lora_A["default"].weight.copy_(torch.randn_like(core.linear.lora_A["default"].weight) * .1)
            core.linear.lora_B["default"].weight.copy_(torch.randn_like(core.linear.lora_B["default"].weight) * .1)
            active_duration = core.duration_predictor.modules_to_save["default"]
            active_duration.weight.copy_(torch.randn_like(active_duration.weight) * .1)
            active_duration.bias.copy_(torch.randn_like(active_duration.bias) * .1)
        frozen = Tiny().to(dtype=torch.bfloat16).eval()
        frozen.linear = frozen_linear(copy.deepcopy(core.linear.base_layer),
            core.linear.lora_A["default"].weight.detach().clone(), core.linear.lora_B["default"].weight.detach().clone(), scale=2.0)
        for key, value in active_duration.state_dict().items():
            assign_tensor(frozen, "duration_predictor." + key, value.clone(), device="cpu", dtype=torch.bfloat16)
        self.assertTrue(torch.equal(frozen.linear.base_layer.weight, core.linear.base_layer.weight))
        self.assertTrue(torch.equal(frozen.linear.lora_A.weight, core.linear.lora_A["default"].weight))
        self.assertTrue(torch.equal(frozen.linear.lora_B.weight, core.linear.lora_B["default"].weight))
        for key, value in active_duration.state_dict().items():
            self.assertTrue(torch.equal(frozen.duration_predictor.state_dict()[key], value), key)
        with torch.inference_mode():
            for shape in ((3, 8), (2, 4, 8)):
                x = torch.randn(shape, dtype=torch.bfloat16)
                self.assertTrue(torch.equal(baseline(x), frozen(x)))
        self.assertFalse(torch.cuda.is_initialized())

    def test_real_fixed_manifest_has_complete_allowlisted_pairs_and_duration(self):
        from .frozen_lora import fixed_manifest, PINNED_DIRECTORY
        from runtime import irodori_lowmem as low
        low.configure_paths(_CONFIG)
        MODEL = low.MODEL
        if not MODEL.is_file() or not (PINNED_DIRECTORY / "adapter_model.safetensors").is_file():
            self.skipTest("Private adapter/base artifacts are not present")
        from unittest.mock import patch
        manifest = fixed_manifest(PINNED_DIRECTORY, MODEL)
        self.assertEqual(len(manifest["pairs"]), 132)
        self.assertEqual(len(manifest["duration"]), 31)
        with self.assertRaises(ValueError):
            fixed_manifest(PINNED_DIRECTORY.parent, MODEL)
        with patch("desktop_pet.voice.frozen_lora._hash", return_value="0" * 64):
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                fixed_manifest(PINNED_DIRECTORY, MODEL)

    def test_frozen_linear_rejects_unsupported_layers_shapes_and_dtypes(self):
        import torch
        from .frozen_lora import frozen_linear
        base = torch.nn.Linear(8, 6, bias=False, dtype=torch.bfloat16)
        a = torch.zeros(2, 8, dtype=torch.bfloat16)
        b = torch.zeros(6, 2, dtype=torch.bfloat16)
        with self.assertRaises(ValueError):
            frozen_linear(torch.nn.Conv1d(8, 6, 1), a, b, scale=2.0)
        with self.assertRaises(ValueError):
            frozen_linear(base, a[:, :7], b, scale=2.0)
        with self.assertRaises(ValueError):
            frozen_linear(base, a, b[:5], scale=2.0)
        with self.assertRaises(ValueError):
            frozen_linear(base, a.float(), b, scale=2.0)


if __name__ == "__main__":
    unittest.main()
