"""Exact local voice history. Replaying never asks an Agent or a voice model."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import time
import unicodedata
import wave


def _spoken_key(text: str) -> str:
    # Irodori normalizes full-width punctuation and ellipses before speaking.
    return "".join(unicodedata.normalize("NFKC", text).split())


def _timestamp(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (ValueError, TypeError):
        return time.time()


class ReplayStore:
    """Bound recent attempts, pin explicitly selected favorites with byte copies.

    Entries identify an Agent reply AND its voice operation. Identical text in
    another turn, or a new voice retry, is a separate recorded performance.
    """

    def __init__(self, directory: str | Path, *, workspace: str | Path, recent_limit=80):
        self.workspace = Path(workspace).resolve()
        self.directory = self._inside(directory)
        self.path = self.directory / "index.json"
        self.recent_limit = max(1, int(recent_limit))
        self._records: dict[str, dict] = {}
        self._operations: dict[str, str] = {}
        self._hash_cache: dict[str, tuple] = {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            for record in value.get("records", []):
                if record.get("id") == self._identity(record["agent_turn_id"], record["operation_id"]):
                    self._records[record["id"]] = record
                    self._operations[record["operation_id"]] = record["id"]
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def _inside(self, path) -> Path:
        path = Path(path)
        path = (path if path.is_absolute() else self.workspace / path).resolve()
        if not path.is_relative_to(self.workspace):
            raise ValueError("Voice library paths must stay inside the workspace.")
        return path

    @staticmethod
    def _identity(agent_turn_id, operation_id) -> str:
        return hashlib.sha256(f"{agent_turn_id}\0{operation_id}".encode()).hexdigest()[:32]

    def _save(self):
        ordered = sorted(self._records.values(), key=lambda item: item["created_at"], reverse=True)
        keep = {item["id"] for item in ordered[:self.recent_limit]}
        self._records = {item["id"]: item for item in ordered if item.get("favorite") or item["id"] in keep}
        self._operations = {item["operation_id"]: item["id"] for item in self._records.values()}
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".part")
        temporary.write_text(json.dumps({"schema": "desktop-pet-voice-library-v1",
                                         "records": list(self._records.values())},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def add_reply(self, reply: dict, operation_id: str, *, profile="quality_40", created_at=None,
                  persist=True) -> str | None:
        if reply.get("provider_mode") not in {"codex_subscription", "local_memory"}:
            return None
        agent_turn_id = reply.get("turn_id")
        if not agent_turn_id or not operation_id:
            return None
        record_id = self._identity(agent_turn_id, operation_id)
        record = self._records.setdefault(record_id, {
            "id": record_id, "agent_turn_id": agent_turn_id, "operation_id": operation_id,
            "created_at": _timestamp(created_at) if created_at is not None else time.time(),
            "favorite": False, "pieces": [], "generation_finished": False,
        })
        record.update(reply=deepcopy(reply), profile=profile,
                      speech_text_ja=reply.get("speech_text_ja", ""),
                      subtitle_zh=reply.get("subtitle_zh", ""), caption_ja=reply.get("caption_ja", ""))
        self._operations[operation_id] = record_id
        if persist:
            self._save()
        return record_id

    def begin_retry(self, operation_id: str, agent_turn_id: str, *, profile="quality_40",
                    created_at=None, persist=True) -> str | None:
        source = next((item for item in self._records.values() if item["agent_turn_id"] == agent_turn_id), None)
        if source:
            return self.add_reply(source["reply"], operation_id, profile=profile,
                                  created_at=created_at, persist=persist)
        return None

    def add_result(self, operation_id: str, result: dict, *, persist=True):
        record = self._records.get(self._operations.get(operation_id))
        if (record is None or result.get("status") != "generated" or not result.get("playable")
                or not result.get("wav_path")):
            return
        try:
            path = self._inside(result["wav_path"])
            segment = int(result.get("segment_id", len(record["pieces"])))
        except (ValueError, TypeError):
            return
        if segment < 0:
            return
        audio = deepcopy(result.get("audio") or {})
        piece = {"segment_id": str(segment), "index": segment,
                 "request_id": result.get("request_id"), "source_wav_path": str(path),
                 "wav_path": str(path), "text_spoken_ja": result.get("text_spoken_ja", ""),
                 "sha256": audio.get("sha256"), "audio": audio,
                 "profile": result.get("synthesis_profile", result.get("profile", record["profile"])),
                 "model_sha256": result.get("model_sha256"),
                 "adapter_sha256": result.get("adapter_sha256") or (result.get("cache") or {}).get("adapter_sha256"),
                 "model_path": result.get("model_path"), "adapter_path": result.get("adapter_path"),
                 "upstream_commit": result.get("upstream_commit"),
                 "caption_ja": result.get("caption_ja", record["caption_ja"]),
                 "created_at": result.get("created_utc", result.get("created_at")),
                 "watermark": result.get("watermark_processing")}
        existing = next((old for old in record["pieces"] if old["index"] == segment), None)
        if existing and existing.get("favorite_wav_path"):
            # An index re-import cannot overwrite the explicitly saved performance.
            piece["favorite_wav_path"] = existing["favorite_wav_path"]
        record["pieces"] = sorted([old for old in record["pieces"] if old["index"] != segment] + [piece],
                                  key=lambda item: item["index"])
        if persist:
            self._save()

    def finish(self, operation_id, *, persist=True):
        record = self._records.get(self._operations.get(operation_id))
        if record:
            record["generation_finished"] = True
            if persist:
                self._save()

    def import_events(self, paths):
        # Stream immutable events instead of trusting mutable request metadata,
        # which can say cancelled after the WAV was successfully played.
        retries = []
        for filename in dict.fromkeys(str(self._inside(path)) for path in paths):
            try:
                with Path(filename).open(encoding="utf-8-sig") as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                            kind = event.get("event", event.get("type"))
                            operation = event.get("turn_id")
                            if kind in {"reply", "proactive_reply"}:
                                self.add_reply(event["reply"], operation, profile=event.get("profile", "quality_40"),
                                               created_at=event.get("time"), persist=False)
                            elif kind == "voice_retry_started":
                                retries.append(event)
                                self.begin_retry(operation, event["agent_turn_id"], profile=event.get("profile", "quality_40"),
                                                 created_at=event.get("time"), persist=False)
                            elif kind == "voice_result":
                                self.add_result(operation, event["result"], persist=False)
                            elif kind == "turn_finished":
                                self.finish(operation, persist=False)
                        except (ValueError, TypeError, KeyError):
                            continue
            except OSError:
                continue
        # Older integration logs sometimes contain only a retry; its real reply
        # can be found in another log. A second pass is needed only for those.
        unresolved = [event for event in retries if event.get("turn_id") not in self._operations]
        if unresolved:
            for event in unresolved:
                self.begin_retry(event["turn_id"], event["agent_turn_id"], profile=event.get("profile", "quality_40"),
                                 created_at=event.get("time"), persist=False)
            wanted = {event["turn_id"] for event in unresolved}
            for filename in paths:
                try:
                    with self._inside(filename).open(encoding="utf-8-sig") as stream:
                        for line in stream:
                            try:
                                event = json.loads(line)
                                if event.get("turn_id") in wanted and event.get("event", event.get("type")) == "voice_result":
                                    self.add_result(event["turn_id"], event["result"], persist=False)
                            except (ValueError, TypeError, KeyError):
                                continue
                except OSError:
                    continue
        self._save()

    def _available_path(self, piece: dict) -> Path | None:
        for value in (piece.get("favorite_wav_path"), piece.get("wav_path")):
            if not value:
                continue
            try:
                path = self._inside(value)
                stat = path.stat()
                signature = (stat.st_size, stat.st_mtime_ns)
                cached = self._hash_cache.get(str(path))
                if cached is None or cached[:2] != signature:
                    with path.open("rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    with wave.open(str(path), "rb") as stream:
                        if stream.getcomptype() != "NONE" or stream.getsampwidth() not in (1, 2, 3, 4):
                            continue
                    cached = (*signature, digest)
                    self._hash_cache[str(path)] = cached
                if not piece.get("sha256") or piece["sha256"] == cached[2]:
                    return path
            except (OSError, ValueError, wave.Error, EOFError):
                continue
        return None

    def get(self, record_id: str) -> dict | None:
        source = self._records.get(record_id)
        if source is None:
            return None
        record = deepcopy(source)
        pieces = record["pieces"]
        for piece in pieces:
            path = self._available_path(piece)
            piece["playback_path"] = str(path) if path else None
        record["available_pieces"] = sum(bool(piece["playback_path"]) for piece in pieces)
        full_text = _spoken_key(record["speech_text_ja"])
        contiguous = [piece["index"] for piece in pieces] == list(range(len(pieces)))
        covered = _spoken_key("".join(piece["text_spoken_ja"] for piece in pieces))
        record["generated_complete"] = bool(pieces and contiguous and covered == full_text)
        record["complete"] = record["generated_complete"] and record["available_pieces"] == len(pieces)
        record["seconds"] = sum(float(piece["audio"].get("seconds") or 0) for piece in pieces if piece["playback_path"])
        return record

    def for_reply(self, agent_turn_id: str) -> dict | None:
        choices = [self.get(item["id"]) for item in self._records.values() if item["agent_turn_id"] == agent_turn_id]
        return max(choices, key=lambda item: (item["complete"], bool(item["available_pieces"]),
                   item["operation_id"] == agent_turn_id, item["created_at"]), default=None)

    def records(self, *, favorites_only=False) -> list[dict]:
        return [self.get(item["id"]) for item in sorted(self._records.values(),
                key=lambda item: item["created_at"], reverse=True) if not favorites_only or item.get("favorite")]

    def playlist(self, record_id: str) -> list[dict]:
        record = self.get(record_id)
        if record is None or not record["available_pieces"]:
            raise FileNotFoundError("這則回覆沒有可重播的原音。")
        return [{"status": "replay", "playable": True, "wav_path": piece["playback_path"],
                 "request_id": piece["request_id"], "segment_id": piece["segment_id"],
                 "text_spoken_ja": piece["text_spoken_ja"], "audio": piece["audio"],
                 "original_operation_id": record["operation_id"], "record_id": record_id}
                for piece in record["pieces"] if piece["playback_path"]]

    def set_favorite(self, record_id: str, favorite=True):
        record = self._records[record_id]
        if favorite:
            available = self.get(record_id)
            if not available["available_pieces"]:
                raise FileNotFoundError("這則回覆沒有可收藏的原音。")
            directory = self._inside(self.directory / "favorites" / record_id)
            directory.mkdir(parents=True, exist_ok=True)
            for piece, checked in zip(record["pieces"], available["pieces"]):
                if not checked["playback_path"]:
                    continue
                source = Path(checked["playback_path"])
                destination = self._inside(directory / f"{piece['index']:03d}.wav")
                if source != destination:
                    temporary = destination.with_suffix(".part")
                    shutil.copyfile(source, temporary)
                    if piece.get("sha256"):
                        with temporary.open("rb") as stream:
                            if hashlib.file_digest(stream, "sha256").hexdigest() != piece["sha256"]:
                                raise ValueError("原音在收藏時發生變更，請重新選擇。")
                    temporary.replace(destination)
                piece["favorite_wav_path"] = str(destination)
            record["favorited_at"] = time.time()
        record["favorite"] = bool(favorite)
        # Removing a favorite changes its index only; no audio is deleted.
        self._save()
        return self.get(record_id)
