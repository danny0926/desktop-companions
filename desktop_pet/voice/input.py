"""Optional file-ASR boundary. P0 input is text; mic/VAD are disabled."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import wave
from pathlib import Path

from .resources import GIB, memory_status
from .service import ROOT, load_config


class VoiceInputUnavailable(RuntimeError):
    pass


class VoiceInputService:
    def __init__(self, config_path: str | Path | None = None):
        self.config = load_config(config_path)["voice_input"]

    def health(self) -> dict:
        model = self.config.get("asr_model_path")
        return {**self.config, "available": bool(self.config["enabled"] and model and Path(model).is_dir()),
                "microphone_available": False, "vad_available": False,
                "echo_cancellation": "not_configured", "input_mode": "file_asr" if self.config["enabled"] else "text_p0"}

    def transcribe_file(self, audio_path: str | Path, *, language: str = "ja") -> dict:
        if not self.health()["available"]:
            raise VoiceInputUnavailable("Voice input is disabled. Configure a local small CPU ASR model to enable file input; text remains available.")
        # The existing large-v3 download is deliberately not selected as a
        # default. Running ASR requires a separate host budget and stays on CPU.
        if memory_status().get("physical_available_bytes", 0) < 2 * GIB:
            raise VoiceInputUnavailable("At least 2 GiB host headroom is required before optional CPU ASR.")
        audio = Path(audio_path).resolve()
        with wave.open(str(audio), "rb") as stream:
            if stream.getnframes() / stream.getframerate() > 20:
                raise ValueError("File ASR accepts a WAV of at most 20 seconds.")
        python = Path(self.config["asr_python"])
        if not python.is_absolute():
            python = ROOT / python
        env = dict(os.environ, PYTHONIOENCODING="utf-8", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        result = subprocess.run(
            [str(python), "-m", "desktop_pet.voice.input", "--audio", str(audio),
             "--model", str(self.config["asr_model_path"]), "--language", language],
            cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", timeout=90,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True,
        )
        return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--language", default="ja")
    args = parser.parse_args()
    from faster_whisper import WhisperModel
    model = WhisperModel(args.model, device="cpu", compute_type="int8", cpu_threads=2,
                         num_workers=1, local_files_only=True)
    segments, info = model.transcribe(args.audio, language=args.language, beam_size=1, vad_filter=False)
    print(json.dumps({"text": "".join(segment.text for segment in segments).strip(),
                      "language": info.language, "device": "cpu", "compute_type": "int8",
                      "vad": "disabled", "source": "local_wav"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
