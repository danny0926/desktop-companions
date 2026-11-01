"""Configure optional voice explicitly; no installation, download or inference by default."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop_pet.voice.service import load_config
from runtime.voice_artifacts import LOCK, inspect, sha256


def run(command: list[str]) -> None:
    subprocess.run(command, check=True, cwd=ROOT,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def save_overrides(overrides: dict) -> None:
    path = ROOT / "config/desktop_pet_voice.local.json"
    previous = json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
    previous.update(overrides)
    path.write_text(json.dumps(previous, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved private local configuration: {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check required files; never imports ML.")
    parser.add_argument("--verify-hashes", action="store_true", help="Also read model/adapter/reference files for SHA-256.")
    parser.add_argument("--fetch-upstream", action="store_true", help="Clone official upstream at the pinned commit.")
    parser.add_argument("--download", action="store_true", help="Download pinned public voice artifacts using huggingface-hub.")
    parser.add_argument("--reuse-workspace", type=Path, help="Reference an existing trusted TTS workspace; copy no files.")
    parser.add_argument("--adapter-dir", type=Path, help="User-owned compatible PEFT adapter directory.")
    parser.add_argument("--reference-latent", type=Path, help="User-owned compatible deterministic reference latent.")
    args = parser.parse_args()
    overrides = {}
    if args.reuse_workspace:
        workspace = args.reuse_workspace.resolve(strict=True)
        old_path = workspace / "config/desktop_pet_voice.json"
        old = json.loads(old_path.read_text(encoding="utf-8-sig"))
        for key in ("python", "adapter_path", "reference_latent"):
            path = Path(old[key])
            overrides[key] = str(path if path.is_absolute() else workspace / path)
        overrides.update(adapter_sha256=old["adapter_sha256"], reference_sha256=old["reference_sha256"],
                         upstream_dir=str(workspace / "vendor/irodori-tts"), hf_home=str(workspace / ".cache_hf"),
                         model_path=str(workspace / ".cache_hf/hub/models--Aratako--Irodori-TTS-v4.1-Small/snapshots" / LOCK["base"]["revision"] / "model.safetensors"),
                         codec_path=str(workspace / ".cache_hf/hub/models--Aratako--Semantic-DACVAE-Japanese-32dim/snapshots" / LOCK["codec"]["revision"] / "weights.pth"),
                         watermark_dir=str(workspace / ".cache_hf/hub/models--sony--silentcipher/snapshots" / LOCK["watermark"]["revision"]))
    if args.adapter_dir:
        adapter = args.adapter_dir.resolve(strict=True)
        if not (adapter / "adapter_config.json").is_file():
            raise ValueError("The adapter must include adapter_config.json.")
        overrides.update(adapter_path=str(adapter), adapter_sha256=sha256(adapter / "adapter_model.safetensors"))
    if args.reference_latent:
        reference = args.reference_latent.resolve(strict=True)
        overrides.update(reference_latent=str(reference), reference_sha256=sha256(reference))
    if overrides:
        save_overrides(overrides)
    config = load_config()
    if args.fetch_upstream:
        upstream = Path(config["upstream_dir"])
        if upstream.exists():
            raise ValueError("Upstream destination already exists; inspect it instead of overwriting.")
        upstream.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", "--no-checkout", LOCK["upstream"]["url"], str(upstream)])
        run(["git", "-C", str(upstream), "checkout", "--detach", LOCK["upstream"]["commit"]])
    if args.download:
        from huggingface_hub import snapshot_download
        os.environ["HF_HOME"] = config["hf_home"]
        for key, destination in (("base", Path(config["model_path"]).parent),
                                 ("codec", Path(config["codec_path"]).parent),
                                 ("watermark", Path(config["watermark_dir"]))):
            item = LOCK[key]
            snapshot_download(repo_id=item["repo_id"], revision=item["revision"],
                              allow_patterns=item["include"], local_dir=str(destination),
                              cache_dir=str(Path(config["hf_home"]) / "hub"))
    report = inspect(config, verify_hashes=args.verify_hashes)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Voice setup: {exc}", file=sys.stderr)
        raise SystemExit(2)
