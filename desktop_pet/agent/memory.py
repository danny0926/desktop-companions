"""Small per-character fact store and bounded conversation journal.

Canonical character sources stay in the character config. This database stores
user-authored memory separately from conversation turns; model output is never
promoted into a character fact or a user fact automatically.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator
import uuid


KINDS = ("preference", "fact", "relationship", "note")
SOURCES = ("user_explicit", "user_editor")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _tokens(text: str) -> set[str]:
    lowered = text.casefold()
    words = set(re.findall(r"[a-z0-9_]{2,}", lowered))
    for run in re.findall(r"[\u3040-\u30ff\u3400-\u9fff]+", lowered):
        words.update(run[index:index + 2] for index in range(max(1, len(run) - 1)))
    return words


class MemoryStore:
    def __init__(self, database: str | Path, character_id: str, *, max_fact_chars: int = 1000):
        self.database = Path(database).resolve()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.character_id = character_id
        self.max_fact_chars = max_fact_chars
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS user_memories (
                    id TEXT PRIMARY KEY,
                    character_id TEXT NOT NULL,
                    memory_key TEXT NOT NULL,
                    body TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_turn_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(character_id, memory_key)
                );
                CREATE INDEX IF NOT EXISTS memories_by_character
                    ON user_memories(character_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    input_text TEXT NOT NULL,
                    reply_json TEXT NOT NULL,
                    heard_text TEXT,
                    delivery_status TEXT NOT NULL DEFAULT 'generated',
                    created_at TEXT NOT NULL,
                    UNIQUE(character_id, conversation_id, turn_id)
                );
                CREATE INDEX IF NOT EXISTS turns_by_conversation
                    ON conversation_turns(character_id, conversation_id, sequence DESC);
                CREATE TABLE IF NOT EXISTS proactive_turns (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    character_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    reply_json TEXT,
                    heard_text TEXT,
                    delivery_status TEXT NOT NULL DEFAULT 'generating',
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    UNIQUE(character_id, conversation_id, turn_id),
                    UNIQUE(character_id, event_id)
                );
                CREATE INDEX IF NOT EXISTS proactive_by_conversation
                    ON proactive_turns(character_id, conversation_id, delivered_at DESC);
                PRAGMA user_version=2;
            """)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=3000")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def remember(
        self, text: str, *, key: str | None = None, kind: str = "note",
        source: str = "user_explicit", source_turn_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > self.max_fact_chars:
            raise ValueError(f"memory text must contain 1..{self.max_fact_chars} characters")
        if kind not in KINDS or source not in SOURCES:
            raise ValueError("Unsupported memory kind or source")
        body = text.strip()
        if key is None:
            key = "note:" + hashlib.sha256(body.casefold().encode("utf-8")).hexdigest()[:20]
        if not isinstance(key, str) or not key.strip() or len(key) > 160:
            raise ValueError("memory key must contain 1..160 characters")
        if source_turn_id is not None and (not isinstance(source_turn_id, str) or len(source_turn_id) > 160):
            raise ValueError("source_turn_id must be a string up to 160 characters")
        timestamp = _now()
        with self._connection() as db:
            db.execute("""
                INSERT INTO user_memories
                    (id, character_id, memory_key, body, kind, source, source_turn_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(character_id, memory_key) DO UPDATE SET
                    body=excluded.body, kind=excluded.kind, source=excluded.source,
                    source_turn_id=excluded.source_turn_id, updated_at=excluded.updated_at
            """, (uuid.uuid4().hex, self.character_id, key.strip(), body, kind, source,
                  source_turn_id, timestamp, timestamp))
            row = db.execute(
                "SELECT * FROM user_memories WHERE character_id=? AND memory_key=?",
                (self.character_id, key.strip()),
            ).fetchone()
        return dict(row)

    def list(self, *, limit: int = 200, kind: str | None = None) -> list[dict[str, Any]]:
        limit = min(1000, max(1, int(limit)))
        with self._connection() as db:
            if kind is None:
                rows = db.execute(
                    "SELECT * FROM user_memories WHERE character_id=? ORDER BY updated_at DESC, id LIMIT ?",
                    (self.character_id, limit),
                ).fetchall()
            else:
                if kind not in KINDS:
                    raise ValueError("Unsupported memory kind")
                rows = db.execute(
                    "SELECT * FROM user_memories WHERE character_id=? AND kind=? ORDER BY updated_at DESC, id LIMIT ?",
                    (self.character_id, kind, limit),
                ).fetchall()
        return [dict(row) for row in rows]

    def delete(self, memory_id: str) -> bool:
        with self._connection() as db:
            result = db.execute(
                "DELETE FROM user_memories WHERE character_id=? AND id=?",
                (self.character_id, memory_id),
            )
            return result.rowcount > 0

    def recall(self, query: str, *, limit: int = 6, char_budget: int = 1800) -> list[dict[str, Any]]:
        """Rank a small local store by words/CJK bigrams; no embedding process."""
        candidates = self.list(limit=500)
        query_tokens = _tokens(query)

        def score(item: dict[str, Any]) -> tuple[float, str]:
            tokens = _tokens(item["body"] + " " + item["memory_key"])
            overlap = len(query_tokens & tokens) / max(1, len(query_tokens))
            # Preferences and explicit profile fields remain useful on a new topic.
            profile = 0.12 if item["kind"] in ("preference", "relationship") or item["memory_key"].startswith("user.") else 0
            return overlap + profile, item["updated_at"]

        ranked = sorted(candidates, key=score, reverse=True)
        result: list[dict[str, Any]] = []
        used = 0
        for item in ranked:
            relevance, _ = score(item)
            if relevance <= 0 and result:
                continue
            cost = len(item["body"]) + len(item["memory_key"]) + 80
            if used + cost > char_budget:
                continue
            result.append(item)
            used += cost
            if len(result) >= limit:
                break
        return result

    def get_turn(self, conversation_id: str, turn_id: str) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM conversation_turns WHERE character_id=? AND conversation_id=? AND turn_id=?",
                (self.character_id, conversation_id, turn_id),
            ).fetchone()
        return dict(row) if row else None

    def save_turn(
        self, conversation_id: str, turn_id: str, input_text: str,
        reply: dict[str, Any], *, retain_turns: int = 80,
    ) -> None:
        with self._connection() as db:
            db.execute("""
                INSERT INTO conversation_turns
                    (character_id, conversation_id, turn_id, input_text, reply_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (self.character_id, conversation_id, turn_id, input_text,
                  json.dumps(reply, ensure_ascii=False), _now()))
            if int(retain_turns) > 0:
                db.execute("""
                DELETE FROM conversation_turns WHERE character_id=? AND conversation_id=?
                AND sequence NOT IN (
                    SELECT sequence FROM conversation_turns WHERE character_id=? AND conversation_id=?
                    ORDER BY sequence DESC LIMIT ?
                )
                """, (self.character_id, conversation_id, self.character_id, conversation_id,
                      int(retain_turns)))

    def conversation_history(self, conversation_id: str, *, limit: int = 60,
                             offset: int = 0) -> list[dict[str, Any]]:
        """Return chronological manual turns and actually heard proactive remarks."""
        limit = min(200, max(1, int(limit)))
        offset = max(0, int(offset))
        with self._connection() as db:
            rows = db.execute("""
                SELECT kind, sequence, turn_id, input_text, reply_json, heard_text,
                       delivery_status, created_at, sort_at
                FROM (
                    SELECT 'manual' AS kind, sequence, turn_id, input_text, reply_json,
                           heard_text, delivery_status, created_at, created_at AS sort_at
                    FROM conversation_turns WHERE character_id=? AND conversation_id=?
                    UNION ALL
                    SELECT 'proactive' AS kind, sequence, turn_id, NULL AS input_text,
                           reply_json, heard_text, delivery_status, created_at,
                           COALESCE(delivered_at, created_at) AS sort_at
                    FROM proactive_turns WHERE character_id=? AND conversation_id=?
                        AND delivery_status IN ('delivered', 'interrupted')
                        AND reply_json IS NOT NULL AND heard_text IS NOT NULL AND heard_text!=''
                ) ORDER BY sort_at, kind, sequence LIMIT ? OFFSET ?
            """, (self.character_id, conversation_id, self.character_id, conversation_id,
                  limit, offset)).fetchall()
        result = []
        for row in rows:
            reply = json.loads(row['reply_json']) if row['reply_json'] else {}
            result.append({
                'kind': row['kind'], 'turn_id': row['turn_id'],
                'input_text': row['input_text'], 'reply': reply,
                'heard_text': row['heard_text'], 'delivery_status': row['delivery_status'],
                'created_at': row['created_at'],
            })
        return result

    def session_info(self, conversation_id: str) -> dict[str, int | str]:
        with self._connection() as db:
            manual = db.execute("SELECT COUNT(*) FROM conversation_turns WHERE character_id=? AND conversation_id=?",
                                (self.character_id, conversation_id)).fetchone()[0]
            proactive = db.execute("""SELECT COUNT(*) FROM proactive_turns
                WHERE character_id=? AND conversation_id=? AND delivery_status IN ('delivered','interrupted')
                    AND reply_json IS NOT NULL AND heard_text IS NOT NULL AND heard_text!=''""",
                                  (self.character_id, conversation_id)).fetchone()[0]
            facts = db.execute("SELECT COUNT(*) FROM user_memories WHERE character_id=?",
                               (self.character_id,)).fetchone()[0]
        return {'character_id': self.character_id, 'conversation_id': conversation_id,
                'manual_count': manual, 'proactive_count': proactive,
                'history_count': manual + proactive, 'memory_count': facts}

    def recent_messages(
        self, conversation_id: str, *, turn_limit: int = 8, char_budget: int = 5000,
        provider_mode: str | None = None,
    ) -> list[dict[str, str]]:
        with self._connection() as db:
            rows = db.execute("""
                SELECT 'dialogue' AS turn_type, sequence, input_text, reply_json,
                    heard_text, delivery_status, NULL AS event_json, created_at AS context_at
                FROM conversation_turns WHERE character_id=? AND conversation_id=?
                UNION ALL
                SELECT 'proactive' AS turn_type, sequence, NULL AS input_text, reply_json,
                    heard_text, delivery_status, event_json, delivered_at AS context_at
                FROM proactive_turns WHERE character_id=? AND conversation_id=?
                    AND delivery_status IN ('delivered', 'interrupted')
                    AND reply_json IS NOT NULL AND heard_text IS NOT NULL AND heard_text!=''
                ORDER BY context_at DESC, sequence DESC LIMIT ?
            """, (self.character_id, conversation_id, self.character_id, conversation_id,
                  max(1, turn_limit))).fetchall()
        pairs: list[list[dict[str, str]]] = []
        used = 0
        for row in rows:
            reply = json.loads(row["reply_json"])
            if reply.get("provider_mode") == "local_memory":
                # Memory editor commands are not dialogue. Forgetting an item
                # must not re-inject its original /remember text on the next turn.
                continue
            # Demonstration lines must not become a real provider's fictional history.
            if provider_mode and provider_mode != "offline_demo" and reply.get("provider_mode") == "offline_demo":
                continue
            if row["turn_type"] == "proactive":
                provenance = {"context_type": "delivered_proactive_observation",
                              "event": json.loads(row["event_json"]),
                              "note": "System observation preceding an actually delivered assistant remark; not a user utterance."}
                pair = [{"role": "system", "content": json.dumps(provenance, ensure_ascii=False)}]
            else:
                pair = [{"role": "user", "content": row["input_text"]}]
            if row["delivery_status"] != "discarded":
                heard = row["heard_text"] if row["heard_text"] is not None else reply["speech_text_ja"]
                if heard:
                    pair.append({"role": "assistant", "content": heard})
            cost = sum(len(message["content"]) for message in pair)
            if used + cost > char_budget:
                break
            pairs.append(pair)
            used += cost
        return [message for pair in reversed(pairs) for message in pair]

    def record_delivery(
        self, conversation_id: str, turn_id: str, *, heard_text: str | None = None,
        status: str = "delivered",
    ) -> bool:
        if status not in ("delivered", "interrupted", "discarded"):
            raise ValueError("status must be delivered, interrupted, or discarded")
        if heard_text is not None and (not isinstance(heard_text, str) or len(heard_text) > 8000):
            raise ValueError("heard_text must be a string up to 8000 characters")
        if status == "discarded":
            heard_text = ""
        elif status == "interrupted" and heard_text is None:
            heard_text = ""
        with self._connection() as db:
            result = db.execute("""
                UPDATE conversation_turns SET heard_text=?, delivery_status=?
                WHERE character_id=? AND conversation_id=? AND turn_id=?
            """, (heard_text, status, self.character_id, conversation_id, turn_id))
            if result.rowcount:
                return True
            row = db.execute("""
                SELECT reply_json, delivery_status FROM proactive_turns
                WHERE character_id=? AND conversation_id=? AND turn_id=?
            """, (self.character_id, conversation_id, turn_id)).fetchone()
            if row is None or row["reply_json"] is None or row["delivery_status"] in (
                "cancelled", "discarded", "expired", "failed",
            ):
                return False  # A late delivery ACK cannot resurrect a terminal event.
            reply = json.loads(row["reply_json"])
            actual = heard_text if heard_text is not None else reply["speech_text_ja"]
            if status == "discarded" or not actual:
                stored_reply = None
                actual = ""
            else:
                if actual != reply["speech_text_ja"] or status == "interrupted":
                    reply["speech_text_ja"] = actual
                    reply["subtitle_zh"] = None
                stored_reply = json.dumps(reply, ensure_ascii=False)
            db.execute("""
                UPDATE proactive_turns SET reply_json=?, heard_text=?, delivery_status=?, delivered_at=?
                WHERE character_id=? AND conversation_id=? AND turn_id=?
            """, (stored_reply, actual, status, _now(), self.character_id, conversation_id, turn_id))
            return True

    def get_proactive_turn(
        self, conversation_id: str, turn_id: str, *, event_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self._connection() as db:
            if event_id is not None:
                row = db.execute("SELECT * FROM proactive_turns WHERE character_id=? AND event_id=?",
                                 (self.character_id, event_id)).fetchone()
            else:
                row = db.execute("""
                    SELECT * FROM proactive_turns WHERE character_id=? AND conversation_id=? AND turn_id=?
                """, (self.character_id, conversation_id, turn_id)).fetchone()
        return dict(row) if row else None

    def reserve_proactive_turn(
        self, conversation_id: str, turn_id: str, event: dict[str, Any], *, retain_turns: int = 80,
    ) -> None:
        with self._connection() as db:
            db.execute("""
                INSERT INTO proactive_turns
                    (character_id, conversation_id, turn_id, event_id, event_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (self.character_id, conversation_id, turn_id, event["event_id"],
                  json.dumps(event, ensure_ascii=False, sort_keys=True), _now()))
            if int(retain_turns) > 0:
                db.execute("""
                DELETE FROM proactive_turns WHERE character_id=? AND conversation_id=?
                AND sequence NOT IN (
                    SELECT sequence FROM proactive_turns WHERE character_id=? AND conversation_id=?
                    ORDER BY sequence DESC LIMIT ?
                )
                """, (self.character_id, conversation_id, self.character_id, conversation_id,
                      int(retain_turns)))

    def save_proactive_reply(self, conversation_id: str, turn_id: str, reply: dict[str, Any]) -> None:
        with self._connection() as db:
            result = db.execute("""
                UPDATE proactive_turns SET reply_json=?, delivery_status='generated'
                WHERE character_id=? AND conversation_id=? AND turn_id=? AND delivery_status='generating'
            """, (json.dumps(reply, ensure_ascii=False), self.character_id, conversation_id, turn_id))
            if not result.rowcount:
                raise ValueError("Proactive event is no longer eligible to store a reply")

    def discard_proactive_turn(self, conversation_id: str, turn_id: str, *, status: str = "cancelled") -> bool:
        if status not in ("cancelled", "expired", "failed", "discarded"):
            raise ValueError("Unsupported proactive terminal status")
        with self._connection() as db:
            result = db.execute("""
                UPDATE proactive_turns SET reply_json=NULL, heard_text='', delivery_status=?
                WHERE character_id=? AND conversation_id=? AND turn_id=?
                    AND delivery_status IN ('generating', 'generated')
            """, (status, self.character_id, conversation_id, turn_id))
            return result.rowcount > 0

    def list_proactive_turns(self, *, limit: int = 80) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute("""
                SELECT * FROM proactive_turns WHERE character_id=? ORDER BY sequence DESC LIMIT ?
            """, (self.character_id, min(1000, max(1, int(limit))))).fetchall()
        return [dict(row) for row in rows]

    def discard_pending_proactive(self, turn_id: str | None = None) -> int:
        """Redact generated but undelivered replies after a shell stop/takeover."""
        with self._connection() as db:
            statement = """
                UPDATE proactive_turns SET reply_json=NULL, heard_text='', delivery_status='cancelled'
                WHERE character_id=? AND delivery_status IN ('generating', 'generated')
            """
            parameters: tuple[Any, ...] = (self.character_id,)
            if turn_id is not None:
                statement += " AND turn_id=?"
                parameters += (turn_id,)
            return db.execute(statement, parameters).rowcount

    def clear_conversation(self, conversation_id: str) -> int:
        """Clear recent/journal turns without deleting long-term user memories."""
        with self._connection() as db:
            result = db.execute(
                "DELETE FROM conversation_turns WHERE character_id=? AND conversation_id=?",
                (self.character_id, conversation_id),
            )
            proactive = db.execute("DELETE FROM proactive_turns WHERE character_id=? AND conversation_id=?",
                                   (self.character_id, conversation_id))
            return result.rowcount + proactive.rowcount

    def count(self) -> int:
        with self._connection() as db:
            return int(db.execute(
                "SELECT COUNT(*) FROM user_memories WHERE character_id=?", (self.character_id,),
            ).fetchone()[0])
