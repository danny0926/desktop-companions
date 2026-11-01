"""Clearly labelled, fixed original Japanese lines for wiring the UI offline.

This module does not pretend to run a language model or to understand arbitrary
user input. Keyword selection only chooses one of the following authored lines.
"""

from __future__ import annotations

import re
from typing import Any

from desktop_pet.contracts import ATTITUDES, DELIVERIES, INTENSITIES


_LINES: dict[str, tuple[str, str, str]] = {
    "daily": ("おかえり。少しひと息つきましょ。今日はどんな一日だった？", "回來啦。先歇一會吧，今天過得怎麼樣？", "natural"),
    "gentle": ("ずいぶん頑張ったのね。今は肩の力を抜いて。私もここで、少し一緒に休むから。", "你很努力了。先放鬆一下，我也在這裡陪你歇一會。", "soft"),
    "affectionate": ("あなたとこうして過ごす時間、私けっこう好きなのよ。もう少し、そばにいて。", "我很喜歡這樣和你相處的時間。再陪我一下。", "soft"),
    "playful": ("また私のほう見てたでしょ。そんなに気になるなら、少しだけ話し相手になってあげる。", "你剛才又在看我吧？既然這麼在意，就陪你聊一會。", "teasing"),
    "cold": ("今は少し、考える時間をちょうだい。落ち着いたら、ちゃんと話すわ。", "先給我一點思考的時間。等冷靜後，我會好好說的。", "quiet"),
    "angry": ("それはちょっと、聞き流せないわね。どういうつもりだったのか、ちゃんと聞かせて。", "這件事我可不能輕輕帶過。好好說說你是怎麼想的。", "firm"),
    "surprised": ("えっ、もうできたの？ 思ったより早かったじゃない。ちょっと見せて。", "咦，已經完成了？比我想得還快，讓我看看。", "bright"),
    "sad": ("今日は少し、うまく笑えないかも。何も言わなくていいから、隣にいてくれる？", "今天可能有點笑不出來。不用說什麼，陪在我旁邊好嗎？", "quiet"),
    "sleepy": ("ふう、少し眠くなってきた。あとひと息ついたら、今日はゆっくり休みましょ。", "呼，有點睏了。再歇一會，今天就早點休息吧。", "quiet"),
    "shy": ("そんなふうに言われたら、照れるじゃない。でも、うれしいわ。もう一回だけ聞かせて。", "被你這樣說，會害羞的。不過我很開心，再說一次給我聽。", "soft"),
}


def demo_content(input_text: str) -> dict[str, Any]:
    attitude = "daily"
    intensity = "light"
    delivery: str | None = None
    preview = re.fullmatch(r"/demo\s+(\w+)(?:\s+(\w+))?(?:\s+(\w+))?", input_text.strip())
    if preview:
        chosen, strength, voice = preview.groups()
        if chosen not in ATTITUDES or strength and strength not in INTENSITIES or voice and voice not in DELIVERIES:
            raise ValueError("Use /demo <attitude> [light|medium|strong] [delivery]")
        attitude, intensity, delivery = chosen, strength or "light", voice
    else:
        text = input_text.casefold()
        for words, candidate in (
            (("晚安", "睡", "眠", "おやすみ", "sleep"), "sleepy"),
            (("累", "辛苦", "つかれ", "疲れ", "つらい", "難過", "难过", "tired", "sad"), "gentle"),
            (("喜歡", "喜欢", "愛你", "爱你", "好き", "love"), "affectionate"),
            (("害羞", "照れ", "shy"), "shy"),
            (("逗", "からか", "playful"), "playful"),
            (("生氣", "生气", "怒って", "angry"), "angry"),
            (("完成", "做完", "できた", "終わった", "done"), "surprised"),
        ):
            if any(word in text for word in words):
                attitude = candidate
                break
    spoken, subtitle, default_delivery = _LINES[attitude]
    return {
        "speech_text_ja": spoken, "subtitle_zh": "【離線示範・固定原創台詞】" + subtitle,
        "attitude": attitude, "intensity": intensity, "delivery": delivery or default_delivery,
    }
