"""Map bounded acting choices to TTS captions and sprite actions."""

from __future__ import annotations

import re
from typing import Any, Mapping

from desktop_pet.contracts import (
    ATTITUDES, DELIVERIES, INTENSITIES, AgentReply,
)


_ATTITUDE = {
    "daily": ("気取らない日常会話の調子で", "neutral", "idle"),
    "gentle": ("相手を気づかう優しい気持ちで", "smile", "nod"),
    "affectionate": ("親しい相手に素直な愛情を込めて", "smile", "nod"),
    "playful": ("親しい相手を少しからかう、いたずらっぽい気持ちで", "smug", "adjust_ribbon"),
    "cold": ("距離を置いた冷静な気持ちで", "irritated", "idle"),
    "angry": ("不満を率直に伝える気持ちで", "irritated", "shake"),
    "surprised": ("思いがけないことに驚いた気持ちで", "surprised", "bounce"),
    "sad": ("悲しさを静かににじませて", "sad", "idle"),
    "sleepy": ("眠気を感じたくつろいだ気持ちで", "sleepy", "yawn"),
    "shy": ("親しい相手への照れを少しにじませて", "shy", "adjust_ribbon"),
}
_INTENSITY = {
    "light": "感情の強さは控えめに、落ち着いた声量で",
    "medium": "感情をはっきり表し、声量は会話の範囲に保って",
    "strong": "感情を強く表し、言葉に十分な抑揚をつけて",
}
_DELIVERY = {
    "natural": "自然な速さと間で話す。",
    "soft": "柔らかな声で、語尾を優しく話す。",
    "teasing": "軽い笑みを含む声で、遊び心のある間をつけて話す。",
    "firm": "発音を明瞭にし、言葉の芯を保って話す。",
    "quiet": "近くの相手に届く穏やかな声で、ゆっくり話す。",
    "bright": "明るく軽やかな声で、弾むように話す。",
}
_KANA = re.compile(r"[\u3041-\u3096\u30a1-\u30fa]")
_CONTROL = re.compile(
    r"[\[\]{}<>]|\*[^*\n]{1,80}\*|【[^】\n]{1,120}】|(?:attitude|intensity|delivery|caption_ja|speech_text_ja)\s*[:=]|"
    r"(?:^|\n)\s*(?:二乃|中野二乃|Nino)\s*[:：]|[（(][^）)]{0,50}[）)]",
    re.IGNORECASE,
)


class ReplyFormatError(ValueError):
    """A model response cannot safely be used as a spoken reply."""


def performance(attitude: str, intensity: str, delivery: str) -> dict[str, str]:
    """Intensity changes emotion, not an automatic instruction to shout."""
    if attitude not in ATTITUDES or intensity not in INTENSITIES or delivery not in DELIVERIES:
        raise ReplyFormatError("Unknown acting choice")
    description, expression, motion = _ATTITUDE[attitude]
    if attitude == "playful" and delivery == "bright":
        expression, motion = "laughing", "wave"
    elif attitude == "daily" and delivery == "bright":
        expression, motion = "curious", "wave"
    elif attitude == "daily" and delivery == "quiet":
        motion = "stretch"
    elif attitude == "gentle" and delivery in {"soft", "quiet"}:
        expression = "worried" if intensity != "light" else "smile"
    elif attitude == "affectionate" and delivery == "bright":
        motion = "wave"
    elif attitude == "angry" and intensity == "strong":
        expression = "annoyed"
    elif attitude == "surprised" and intensity == "light":
        expression, motion = "curious", "nod"
    return {
        "caption_ja": f"{description}、{_INTENSITY[intensity]}、{_DELIVERY[delivery]}",
        "expression": expression,
        "motion": motion,
    }


def build_reply(
    value: Mapping[str, Any], *, turn_id: str, provider_mode: str,
    max_speech_chars: int = 260,
) -> AgentReply:
    """Validate spoken Japanese; derive captions/actions from an allowlist.

    Model-supplied turn ids, captions, expressions and motions are ignored.
    """
    text = value.get("speech_text_ja")
    if not isinstance(text, str) or not text.strip():
        raise ReplyFormatError("speech_text_ja must be a nonempty string")
    text = text.strip()
    if len(text) > max_speech_chars:
        raise ReplyFormatError(f"speech_text_ja exceeds {max_speech_chars} characters")
    if not _KANA.search(text):
        raise ReplyFormatError("speech_text_ja must contain Japanese kana")
    if _CONTROL.search(text) or "```" in text:
        raise ReplyFormatError("speech_text_ja contains control fields or stage directions")
    if any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise ReplyFormatError("speech_text_ja contains control characters")
    attitude = value.get("attitude", "daily")
    intensity = value.get("intensity", "light")
    delivery = value.get("delivery", "natural")
    subtitle = value.get("subtitle_zh")
    if subtitle is not None:
        if not isinstance(subtitle, str) or len(subtitle) > 800:
            raise ReplyFormatError("subtitle_zh must be a short string or null")
        subtitle = subtitle.strip() or None
    acting = performance(attitude, intensity, delivery)
    return AgentReply(
        turn_id=turn_id, speech_text_ja=text, subtitle_zh=subtitle,
        attitude=attitude, intensity=intensity, delivery=delivery,
        provider_mode=provider_mode, **acting,
    )


def reply_schema(max_speech_chars: int = 260) -> dict[str, Any]:
    """The same small schema is used by Ollama and capable compatible servers."""
    return {
        "type": "object",
        "properties": {
            "speech_text_ja": {"type": "string", "minLength": 1, "maxLength": max_speech_chars},
            "subtitle_zh": {"type": ["string", "null"]},
            "attitude": {"type": "string", "enum": list(ATTITUDES)},
            "intensity": {"type": "string", "enum": list(INTENSITIES)},
            "delivery": {"type": "string", "enum": list(DELIVERIES)},
        },
        "required": ["speech_text_ja", "subtitle_zh", "attitude", "intensity", "delivery"],
        "additionalProperties": False,
    }
