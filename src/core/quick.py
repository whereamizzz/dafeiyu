"""本地秒回与跟梗：全部零 API 调用。

- quick_reply：私聊/被叫到时，寒暄、报时、摇骰子、抛硬币、冷笑话本地直出；
- detect_echo：群里短句被复读时，本地跟一句「+1」。
"""

from __future__ import annotations

import random
from datetime import datetime

WEEKDAYS = ("一", "二", "三", "四", "五", "六", "日")

GREETINGS = {
    "在吗": ("在，说。", "在。", "嗯？什么事。"),
    "在不在": ("在。", "在，怎么了。"),
    "早": ("早。", "早，吃了没。"),
    "早上好": ("早。", "早，今天有雨没。"),
    "晚安": ("晚安。", "去睡吧，明天聊。"),
    "睡了": ("睡吧。", "晚安。"),
    "你好": ("你好。", "嗯，你好。"),
    "谢谢": ("不用谢。", "顺手的事。"),
    "再见": ("行，回见。", "走了。"),
}

JOKES = (
    "程序员最讨厌的两件事：写注释，和别人不写注释。",
    "我昨天试图早睡，结果失败了，今天试图早起，也失败了。",
    "鲸鱼为什么不玩手机？因为深海没信号。",
    "我减肥了，目标是回到上一次减肥前的体重。",
)

ECHO_REPLIES = ("+1", "复读机是吧", "你们俩商量好的？", "我也这么觉得", "行，都这么说")


class QuickReplies:
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    def match(self, text: str) -> str | None:
        if not self.cfg.enabled:
            return None
        content = text.strip().strip("。！？!?~ ")
        if not content:
            return None

        if any(word in content for word in ("几点", "时间")):
            now = datetime.now()
            return f"{now.hour} 点 {now.minute:02d}。"
        if "星期几" in content or "周几" in content:
            return f"星期{WEEKDAYS[datetime.now().weekday()]}。"
        if "骰子" in content or content.startswith("roll"):
            return f"{random.randint(1, 6)} 点。"
        if "硬币" in content:
            return "正面。" if random.random() < 0.5 else "反面。"
        if "讲个笑话" in content or "说个笑话" in content:
            return random.choice(JOKES)
        for key, replies in GREETINGS.items():
            if content == key:
                return random.choice(replies)
        return None


class EchoDetector:
    """记录最近几条群消息，判断是不是在复读。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._recent: dict[str, list] = {}

    def feed(self, sender: str, content: str, room_id: str = "") -> None:
        bucket = self._recent.setdefault(room_id, [])
        bucket.append((sender, content.strip()))
        if len(bucket) > 6:
            bucket.pop(0)

    def detect(self, sender: str, content: str, room_id: str = "") -> str | None:
        if not self.cfg.enabled:
            return None
        text = content.strip()
        if not text or len(text) > self.cfg.max_chars:
            return None
        recent = self._recent.get(room_id, [])
        same = [
            item for item in recent[:-1]
            if item[1] == text and item[0] != sender
        ]
        if len(same) < 1:
            return None
        if random.random() > self.cfg.probability:
            return None
        return random.choice(ECHO_REPLIES)
