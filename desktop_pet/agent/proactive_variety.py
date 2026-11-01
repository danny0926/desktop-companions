"""Small deterministic guard against recycled optional remarks; no model or I/O."""

from __future__ import annotations

from difflib import SequenceMatcher
import re
import unicodedata


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    # Katakana/hiragana spelling changes do not make a new subject.
    text = "".join(chr(ord(char) - 0x60) if "ァ" <= char <= "ヶ" else char for char in text)
    text = re.sub(r"[\W_]", "", text)
    return re.sub(r"^(?:ねえ|ねぇ|そういえば|ところで|ちょっと)", "", text)


# A handful of the common stock intents that lexical similarity alone misses.
# These describe repetition, never choose a replacement line or topic.
_RECURRING = {
    "パンケーキ": re.compile(r"ぱんけーき|ほっとけーき"),
    "デートの誘い": re.compile(r"でーと|二人で(?:出かけ|お出かけ)"),
    "お茶や休憩の誘い": re.compile(r"お茶|紅茶|こーひー|休憩|[一ひ]と?息|すとれっち|伸び(?:し|を|で)"),
    "前の話の再開": re.compile(r"(?:さっき|この前|前)の(?:話|続き)|話の続き"),
}
_FOOD = re.compile(r"ぱんけーき|ほっとけーき|食べ|料理|夜食|おやつ|甘いもの|しょっぱい|ご飯|献立|お腹|らーめん")


def repetition_reason(candidate: str, heard: list[str]) -> str | None:
    """Reject close paraphrases and sustained stock-topic runs, within a short window."""
    current = _normalise(candidate)
    recent = [_normalise(text) for text in heard[-8:] if text.strip()]
    for previous in recent:
        shorter, longer = sorted((current, previous), key=len)
        if current == previous or (len(shorter) >= 9 and shorter in longer):
            return "最近実際に届いた台詞と同じ内容"
        if min(len(current), len(previous)) >= 9 and SequenceMatcher(None, current, previous).ratio() >= .73:
            return "最近実際に届いた台詞の言い換え"
    for label, pattern in _RECURRING.items():
        # Concrete pancake/date repetitions remain recognisable across several
        # intervening turns. Broad invitations expire sooner to allow natural chat.
        window = recent if label in ("パンケーキ", "デートの誘い") else recent[-3:]
        if pattern.search(current) and any(pattern.search(previous) for previous in window):
            return f"最近繰り返した{label}"
    if _FOOD.search(current) and sum(bool(_FOOD.search(previous)) for previous in recent[-3:]) >= 2:
        return "最近の呼びかけが食べ物の話題に偏っている"
    return None
