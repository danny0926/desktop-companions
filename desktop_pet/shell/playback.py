"""Windows asynchronous WAV playback with measured amplitude mouth frames."""
from __future__ import annotations

from array import array
import math
from pathlib import Path
import sys
import time
import wave
import winsound


class WaveEnvelope:
    step_seconds = 0.06

    def __init__(self, path: str | Path):
        self.levels: list[float] = []
        with wave.open(str(path), "rb") as stream:
            self.duration = stream.getnframes() / stream.getframerate()
            width = stream.getsampwidth()
            count = max(1, round(stream.getframerate() * self.step_seconds))
            if stream.getcomptype() != "NONE" or width not in (1, 2, 3, 4):
                raise ValueError("Expected an uncompressed PCM WAV for playback.")
            while raw := stream.readframes(count):
                if width == 2:
                    samples = array("h", raw)
                    if sys.byteorder != "little":
                        samples.byteswap()
                elif width == 1:
                    samples = [value - 128 for value in raw]
                else:
                    samples = [int.from_bytes(raw[i:i + width], "little", signed=True)
                               for i in range(0, len(raw), width)]
                self.levels.append(math.sqrt(sum(value * value for value in samples) / max(1, len(samples))))
        peak = max(self.levels, default=0.0)
        self.levels = [level / peak if peak else 0.0 for level in self.levels]

    def mouth(self, elapsed: float) -> str:
        index = min(int(elapsed / self.step_seconds), len(self.levels) - 1)
        level = self.levels[index] if index >= 0 else 0.0
        return "talk_open" if level > 0.38 else "talk_small" if level > 0.07 else "idle"


class WavePlayer:
    def __init__(self):
        self.envelope: WaveEnvelope | None = None
        self.started = 0.0
        self.item: dict | None = None

    def play(self, item: dict):
        self.stop()
        envelope = WaveEnvelope(item["result"]["wav_path"])
        winsound.PlaySound(str(item["result"]["wav_path"]),
                           winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        self.item = item
        self.envelope = envelope
        self.started = time.monotonic()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    @property
    def playing(self) -> bool:
        return self.envelope is not None and self.elapsed < self.envelope.duration + 0.08

    def mouth(self) -> str:
        return self.envelope.mouth(self.elapsed) if self.envelope is not None else "idle"

    def stop(self):
        winsound.PlaySound(None, 0)
        self.envelope = self.item = None
