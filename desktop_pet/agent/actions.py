"""Bounded user-authored roleplay actions, separate from spoken dialogue.

These describe an interaction selected or entered by the actual user. They do
not run desktop commands or promise a corresponding sprite animation.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re


MAX_ACTION_CHARS = 80
ACTION_SOURCES = ("text", "mouse", "menu")
_MARKER = "【利用者の実際の動作】"
_ALIASES = {
    "headpat": ("摸頭", "摸头", "摸摸頭", "摸摸头", "拍拍頭", "拍拍头", "輕輕摸頭", "轻轻摸头", "なでなで", "頭を撫でる"),
    "cheek_poke": ("戳臉", "戳脸", "戳戳臉", "戳戳脸", "輕戳臉頰", "轻戳脸颊", "つんつん", "頬をつつく"),
    "shoulder_pat": ("拍肩", "拍拍肩", "輕輕拍肩", "轻轻拍肩", "肩を軽く叩く"),
    "wave": ("打招呼", "揮手", "挥手", "揮揮手", "挥挥手", "手を振る"),
    "hug": ("抱抱", "擁抱", "拥抱", "輕輕抱一下", "轻轻抱一下", "ハグする"),
    "hand_hold": ("牽手", "牵手", "握手", "手をつなぐ"),
}
_KINDS_BY_ALIAS = {alias: kind for kind, aliases in _ALIASES.items() for alias in aliases}


@dataclass(frozen=True)
class UserAction:
    kind: str
    description: str
    source: str = "text"

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", self.kind):
            raise ValueError("action kind must be a short lowercase identifier")
        if self.source not in ACTION_SOURCES:
            raise ValueError("action source must be text, mouse, or menu")
        if not isinstance(self.description, str):
            raise ValueError("action description must be text")
        if any(ord(char) < 32 and not char.isspace() for char in self.description):
            raise ValueError("action description contains control characters")
        description = " ".join(self.description.split())
        if not description or len(description) > MAX_ACTION_CHARS:
            raise ValueError(f"action description must contain 1 to {MAX_ACTION_CHARS} characters")
        object.__setattr__(self, "description", description)


def parse_user_action(text: str) -> UserAction | None:
    """Recognize a complete explicit action; ordinary mentions stay dialogue.

    Invalid/oversized wrappers return None so callers retain the original input.
    Only exact short aliases are recognized without an explicit wrapper.
    """
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text.startswith(_MARKER):
        payload = text[len(_MARKER):]
        if len(payload) > 500:
            return None
        try:
            value = json.loads(payload)
            if not isinstance(value, dict) or set(value) != {"kind", "description", "source"}:
                return None
            return UserAction(**value)
        except (ValueError, TypeError):
            return None

    description: str | None = None
    if re.match(r"^/action\s", text, re.IGNORECASE):
        description = text[len("/action"):].strip()
    elif len(text) >= 3 and (text[0], text[-1]) in (("（", "）"), ("(", ")"), ("*", "*")):
        candidate = text[1:-1].strip()
        # A whole wrapper is an action; mixed dialogue and nested delimiters
        # are left untouched instead of being half-parsed.
        if candidate and not re.search(r"[（）()*\r\n]", candidate):
            description = candidate
    elif text in _KINDS_BY_ALIAS:
        description = text
    if description is None:
        return None
    try:
        return UserAction(_KINDS_BY_ALIAS.get(description, "custom"), description)
    except ValueError:
        return None


def format_user_action(action: UserAction) -> str:
    """Round-trip the actual action source without presenting it as a quote."""
    if not isinstance(action, UserAction):
        raise TypeError("action must be UserAction")
    return _MARKER + json.dumps(
        {"kind": action.kind, "description": action.description, "source": action.source},
        ensure_ascii=False, separators=(",", ":"),
    )
