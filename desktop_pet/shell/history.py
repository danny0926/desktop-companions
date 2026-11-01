"""Small app-internal interface for restoring a character's local session."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from desktop_pet.agent.actions import parse_user_action
from desktop_pet.agent.memory import MemoryStore


class CharacterSession:
    """History and explicit long-term memory access, without loading an agent."""

    def __init__(self, app_config: str | Path | dict[str, Any],
                 agent_config: str | Path | dict[str, Any] | None = None,
                 data_dir: str | Path | None = None,
                 conversation_id: str | None = None):
        app = _read_config(app_config)
        profile = _read_config(agent_config if agent_config is not None
                                else app.get('agent_config', {}))
        if data_dir is None:
            data_dir = app.get('agent_data_dir', '.')
        self.character_id = str(profile.get('character_id', ''))
        if not self.character_id:
            raise ValueError('character config must contain character_id')
        memory_cfg = profile.get('memory', {})
        self.display_name = str(profile.get('display_name') or self.character_id)
        self.conversation_id = str(conversation_id or app.get('conversation_id') or 'desktop-default')
        self.store = MemoryStore(Path(data_dir) / memory_cfg.get('database_name', 'memory.sqlite3'),
                                 self.character_id,
                                 max_fact_chars=int(memory_cfg.get('max_fact_chars', 1000)))

    def load_history(self, *, limit: int = 60, offset: int = 0) -> list[dict[str, Any]]:
        return self.store.conversation_history(self.conversation_id, limit=limit, offset=offset)

    def load_history_parts(self, *, limit: int = 60, offset: int = 0) -> list[tuple]:
        parts: list[tuple] = []
        for record in self.load_history(limit=limit, offset=offset):
            if record['kind'] == 'manual' and record['input_text']:
                action = parse_user_action(record['input_text'])
                if action:
                    parts.append(('action', _action_speaker(action), action.description))
                else:
                    parts.append(('user', '你', record['input_text']))
            reply = record['reply']
            text = (reply.get('display_text_ja') or reply.get('speech_text_ja')
                    if record['kind'] == 'manual'
                    else record.get('heard_text'))
            if text:
                parts.append(('reply', self.display_name, text, record['turn_id']))
                subtitle = reply.get('subtitle_zh') if record['kind'] == 'manual' else None
                if subtitle:
                    parts.append(('subtitle', '', subtitle))
        return parts

    def list_facts(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.list(limit=limit)

    def remember(self, text: str, *, key: str | None = None, kind: str = 'note') -> dict[str, Any]:
        return self.store.remember(text, key=key, kind=kind, source='user_explicit')

    def delete_fact(self, memory_id: str) -> bool:
        return self.store.delete(memory_id)

    def session_info(self) -> dict[str, int | str]:
        return self.store.session_info(self.conversation_id)


def _read_config(value: str | Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    path = Path(value)
    return json.loads(path.read_text(encoding='utf-8'))


def _action_speaker(action) -> str:
    return {'mouse': '你 · 滑鼠互動', 'menu': '你 · 選單互動',
            'text': '你 · 輸入動作'}.get(action.source, '你 · 互動')
