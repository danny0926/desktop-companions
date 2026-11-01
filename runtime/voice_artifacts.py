"""Stdlib-only preflight for optional voice artifacts, without loading weights."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "config/voice_artifacts.lock.json").read_text(encoding="utf-8"))


def required_files(config: dict) -> dict[str, Path]:
    model = Path(config["model_path"])
    watermark = Path(config["watermark_dir"]) / "44_1_khz/73999_iteration"
    return {
        "GPU Python": Path(config["python"]),
        "Irodori upstream": Path(config["upstream_dir"]) / "irodori_tts/inference_runtime.py",
        "base model": model,
        "bundled tokenizer": model.parent / "tokenizer/tokenizer.json",
        "tokenizer config": model.parent / "tokenizer/tokenizer_config.json",
        "codec": Path(config["codec_path"]),
        "adapter weights": Path(config["adapter_path"]) / "adapter_model.safetensors",
        "adapter config": Path(config["adapter_path"]) / "adapter_config.json",
        "reference latent": Path(config["reference_latent"]),
        **{f"SilentCipher {name}": watermark / name for name in
           ("hparams.yaml", "enc_c.ckpt", "dec_c.ckpt", "dec_m_0.ckpt")},
    }


def require_artifacts(config: dict) -> None:
    missing = [(label, path) for label, path in required_files(config).items() if not path.is_file()]
    if missing:
        details = "; ".join(f"{label}: {path}" for label, path in missing)
        raise FileNotFoundError("Voice setup is incomplete. Run python tools/setup_voice.py --check; "
                                "use --no-voice until configured. Missing: " + details)


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for piece in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(piece)
    return result.hexdigest()


def inspect(config: dict, *, verify_hashes: bool = False) -> dict:
    checks = [{"artifact": label, "path": str(path), "exists": path.is_file()}
              for label, path in required_files(config).items()]
    errors = [f"Missing {item['artifact']}: {item['path']}" for item in checks if not item["exists"]]
    model = Path(config["model_path"])
    if model.is_file() and model.stat().st_size != LOCK["base"]["size_bytes"]:
        errors.append("Base model size differs from the pinned checkpoint.")
    upstream = Path(config["upstream_dir"])
    commit = None
    if upstream.is_dir():
        try:
            result = subprocess.run(["git", "-C", str(upstream), "rev-parse", "HEAD"],
                                    capture_output=True, text=True, check=True, timeout=10,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            commit = result.stdout.strip()
            if commit != config["upstream_commit"] or commit != LOCK["upstream"]["commit"]:
                errors.append("Irodori upstream commit differs from the pinned source.")
        except (OSError, subprocess.SubprocessError):
            errors.append("Cannot verify the upstream Git checkout.")
    hashes = {}
    if verify_hashes:
        for label, path, expected in (
            ("base", model, LOCK["base"]["sha256"]),
            ("adapter", Path(config["adapter_path"]) / "adapter_model.safetensors", config["adapter_sha256"]),
            ("reference", Path(config["reference_latent"]), config["reference_sha256"]),
        ):
            if path.is_file():
                hashes[label] = sha256(path)
                if hashes[label] != expected:
                    errors.append(f"{label} SHA-256 differs from the configured artifact.")
    return {"ready": not errors, "checks": checks, "upstream_commit": commit,
            "hashes_verified": verify_hashes, "hashes": hashes, "errors": errors,
            "note": "File preflight only; CUDA, Python dependencies and voice quality are not tested."}
