"""Recover the last genuine reply without calling Codex or changing memory."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
import time

from desktop_pet.contracts import AgentReply


def _timestamp(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (ValueError, TypeError):
        return 0.0


def saved_reply(reply: dict, *, source_time=None, input_text=None, profile="quality_40",
                character_id=None, conversation_id=None) -> dict:
    reply = AgentReply.from_dict(reply)
    if reply.provider_mode not in {"codex_subscription", "local_memory"}:
        raise ValueError("Only actual Agent replies are restored for voice retry.")
    value = {"schema": "desktop-pet-last-reply-v1", "reply": reply.to_dict(),
            "source_time": _timestamp(source_time) if source_time is not None else time.time(),
            "input_text": input_text if isinstance(input_text, str) else None,
            "profile": profile if profile in {"quality_40", "interactive_6_sway"} else "quality_40"}
    if isinstance(character_id, str) and character_id:
        value["character_id"] = character_id
    if isinstance(conversation_id, str) and conversation_id:
        value["conversation_id"] = conversation_id
    return value


def store_last_reply(path: Path, reply: dict, *, input_text=None, profile="quality_40",
                     character_id=None, conversation_id=None) -> dict:
    value = saved_reply(reply, input_text=input_text, profile=profile,
                        character_id=character_id, conversation_id=conversation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return value


def load_last_reply(config: dict) -> dict | None:
    candidates: list[dict] = []
    try:
        character = json.loads(Path(config["agent_config"]).read_text(encoding="utf-8-sig"))
        character_id = character["character_id"]
        conversation_id = config.get("conversation_id", "desktop-default")
        database = Path(config["agent_data_dir"]) / character.get("memory", {}).get("database_name", "memory.sqlite3")
    except (OSError, ValueError, KeyError, TypeError):
        return None

    def turn_belongs(turn_id: str) -> bool:
        if not turn_id:
            return False
        connection = None
        try:
            connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
            return connection.execute(
                    "SELECT 1 FROM conversation_turns WHERE character_id=? AND conversation_id=? AND turn_id=?",
                    (character_id, conversation_id, turn_id),
                ).fetchone() is not None
        except sqlite3.Error:
            return False
        finally:
            if connection is not None:
                connection.close()

    def accept_identity(item: dict, *, legacy_requires_turn: bool) -> bool:
        cached_character = item.get("character_id")
        cached_conversation = item.get("conversation_id")
        if cached_character is not None or cached_conversation is not None:
            return cached_character == character_id and cached_conversation == conversation_id
        return not legacy_requires_turn or turn_belongs(item["reply"]["turn_id"])

    path = Path(config["state_dir"]) / "last-reply.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        item = saved_reply(value["reply"], source_time=value.get("source_time", 0),
                           input_text=value.get("input_text"), profile=value.get("profile"),
                           character_id=value.get("character_id"),
                           conversation_id=value.get("conversation_id"))
        if accept_identity(item, legacy_requires_turn=True):
            candidates.append(item)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    # Compatibility with the pre-retry shell: its existing event log contains
    # the real failed reply. Bound the read rather than loading an ever-growing log.
    try:
        with Path(config["event_log"]).open("rb") as stream:
            stream.seek(0, 2)
            start = max(0, stream.tell() - 1024 * 1024)
            stream.seek(start)
            if start:
                stream.readline()
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
        for line in lines:
            try:
                event = json.loads(line)
                if event.get("event") == "reply":
                    item = saved_reply(event["reply"], source_time=event.get("time", 0),
                                       input_text=event.get("input_text"), profile=event.get("profile"),
                                       character_id=event.get("character_id"),
                                       conversation_id=event.get("conversation_id"))
                    if accept_identity(item, legacy_requires_turn=True):
                        candidates.append(item)
            except (ValueError, KeyError, TypeError):
                continue
    except OSError:
        pass
    # The Agent's existing SQLite journal is the canonical fallback, opened
    # read-only. No schema migration, deletion, generation, or delivery write.
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.5)
        try:
            row = connection.execute(
                "SELECT reply_json,input_text,created_at FROM conversation_turns WHERE character_id=? AND conversation_id=? ORDER BY sequence DESC LIMIT 1",
                (character_id, conversation_id),
            ).fetchone()
        finally:
            connection.close()
        if row:
            candidates.append(saved_reply(json.loads(row[0]), input_text=row[1], source_time=row[2],
                                          character_id=character_id, conversation_id=conversation_id))
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        pass
    if not candidates:
        return None
    newest = max(candidates, key=lambda item: item["source_time"])
    if not newest.get("input_text"):
        newest["input_text"] = next((item["input_text"] for item in candidates if item.get("input_text") and
                                     item["reply"]["turn_id"] == newest["reply"]["turn_id"]), None)
    return newest
