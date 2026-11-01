"""Tiny CPU contracts for the low-RAM path; no checkpoint or GPU is loaded."""
from __future__ import annotations

import sys
from importlib.util import find_spec
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
from .service import load_config
sys.path.insert(0, load_config()["upstream_dir"])


@unittest.skipUnless(all(find_spec(name) is not None for name in ("torch", "dacvae", "irodori_tts")),
                     "Optional voice environment is not installed")
class LoadingContracts(unittest.TestCase):
    def test_decode_only_codec_preserves_decode_and_never_calls_encoder(self):
        import torch
        from dacvae import DACVAE
        from dacvae.nn.bottleneck import VAEBottleneck
        from irodori_tts import inference_runtime as upstream
        from runtime.irodori_lowmem import create_decode_only_codec

        class Tiny(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.sample_rate = 48000
                self.decoder = torch.nn.Identity()
                # Use the installed codec's real weight-normalized Conv1d
                # subclass: it exposes in_channels directly, not .conv.
                self.quantizer = VAEBottleneck(input_dim=1, codebook_dim=3)

            def encode(self, _):
                raise AssertionError("No encoder dummy inference is permitted.")

            def decode(self, latent):
                return self.quantizer.out_proj(latent)

        model = Tiny().eval()
        contracts = {}
        with patch.object(DACVAE, "load", return_value=model):
            codec = create_decode_only_codec(upstream, device="cpu", dtype=torch.float32,
                                            expected_latent_dim=3, contracts=contracts)
        latent = torch.randn(1, 5, 3)
        self.assertTrue(torch.equal(codec.decode_latent(latent), model.decode(latent.transpose(1, 2).contiguous())))
        self.assertFalse(contracts["decode_only_codec"]["encoder_dummy_forward_executed"])
        with self.assertRaises(RuntimeError):
            codec.encode_waveform(torch.zeros(1, 20), 48000)

    def test_shared_backing_keeps_bos_and_text_split_independent(self):
        from irodori_tts.tokenizer import PretrainedTextTokenizer
        backing = SimpleNamespace(pad_token_id=0, bos_token_id=1, padding_side="left",
                                  encode=lambda *args, **kwargs: [2, 3])
        text = PretrainedTextTokenizer(backing, add_bos=True)
        caption = PretrainedTextTokenizer(backing, add_bos=False)
        text.rare_token_splitter = SimpleNamespace(encode=lambda _: [3, 2])
        self.assertIs(text.tokenizer, caption.tokenizer)
        self.assertEqual(text.encode("test").tolist(), [1, 3, 2])
        self.assertEqual(caption.encode("test").tolist(), [2, 3])
        self.assertIsNone(caption.rare_token_splitter)


if __name__ == "__main__":
    unittest.main()
