"""Conservative Japanese reading normalization with no name guessing."""
from __future__ import annotations

import datetime
import re
import unicodedata

_DIGITS = ("", "いち", "に", "さん", "よん", "ご", "ろく", "なな", "はち", "きゅう")
_DAYS = {1: "ついたち", 2: "ふつか", 3: "みっか", 4: "よっか", 5: "いつか", 6: "むいか",
         7: "なのか", 8: "ようか", 9: "ここのか", 10: "とおか", 14: "じゅうよっか",
         17: "じゅうしちにち", 19: "じゅうくにち", 20: "はつか", 24: "にじゅうよっか",
         27: "にじゅうしちにち", 29: "にじゅうくにち"}
_MONTHS = ("", "いち", "に", "さん", "し", "ご", "ろく", "しち", "はち", "く", "じゅう", "じゅういち", "じゅうに")


def _integer(number: int) -> str:
    if number == 0:
        return "れい"
    parts = []
    for power, word, irregular in (
        (1000, "せん", {3: "さんぜん", 8: "はっせん"}),
        (100, "ひゃく", {3: "さんびゃく", 6: "ろっぴゃく", 8: "はっぴゃく"}),
        (10, "じゅう", {}),
    ):
        digit, number = divmod(number, power)
        if digit:
            parts.append(irregular.get(digit, ("" if digit == 1 else _DIGITS[digit]) + word))
    return "".join(parts) + _DIGITS[number]


def normalize_for_speech(text: str, overrides: dict[str, str] | None = None) -> str:
    """Read only explicit complete calendar dates; leave numbers/names alone.

    Recognizes 2026-11-01 and 2026年11月1日. Invalid dates remain unchanged.
    User-configured, literal overrides are the only way to disambiguate names.
    """
    text = unicodedata.normalize("NFKC", text).strip()
    for source, target in sorted((overrides or {}).items(), key=lambda pair: -len(pair[0])):
        if source and isinstance(target, str):
            text = text.replace(source, target)

    def date(match: re.Match) -> str:
        year, month, day = map(int, match.groups())
        try:
            datetime.date(year, month, day)
        except ValueError:
            return match.group(0)
        day_reading = _DAYS.get(day, _integer(day) + "にち")
        year_reading = _integer(year)
        for digit, source, target in ((4, "よん", "よ"), (7, "なな", "しち"), (9, "きゅう", "く")):
            if year % 10 == digit:
                year_reading = year_reading.removesuffix(source) + target
        return year_reading + "ねん" + _MONTHS[month] + "がつ" + day_reading

    text = re.sub(r"(?<![\dA-Za-z])([12]\d{3})-(\d{2})-(\d{2})(?![\dA-Za-z])", date, text)
    return re.sub(r"(?<!\d)([12]\d{3})年(\d{1,2})月(\d{1,2})日(?!\d)", date, text)
