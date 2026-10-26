"""人格与心情：基础人格 + 群人格 + 临时状态 + 语气强度。"""

from __future__ import annotations

import time
from pathlib import Path

from .memory import Memory

TONE_HINTS = {
    1: "语气温和，不咄咄逼人，最多轻轻吐槽一句。",
    2: "语气偏温和，偶尔调侃，但不刻薄。",
    3: "语气中性：正常聊天，该怼就怼，不刻意讨好。",
    4: "语气偏毒舌：爱调侃、爱吐槽，但不对人进行人身攻击。",
    5: "语气毒舌：说话冲、爱阴阳，但不辱骂、不歧视、不越界。",
}

MOOD_BANDS = (
    (0, 15, "心情很差：句子更短，能不接就不接，接了也冷淡，不主动找话题。"),
    (15, 35, "心情偏低：话少，回复简短，不主动开玩笑。"),
    (35, 65, "心情正常：正常聊天，该接就接。"),
    (65, 85, "心情不错：愿意多说两句，偶尔主动搭话。"),
    (85, 101, "心情很好：明显活跃，会开玩笑、接梗，愿意帮人。"),
)

NEGATIVE_WORDS = ("傻", "蠢", "垃圾", "废物", "闭嘴", "烦", "滚", "没用", "智障", "恶心")
POSITIVE_WORDS = ("厉害", "可爱", "牛", "谢谢", "喜欢", "棒", "聪明", "爱了", "好评")


class Persona:
    def __init__(self, cfg, memory: Memory) -> None:
        self.cfg = cfg
        self.memory = memory

    # ---- 文件 ----
    def _read(self, path: str) -> str:
        file = Path(path)
        if not file.exists():
            return ""
        return file.read_text(encoding="utf-8").strip()

    def base(self) -> str:
        return self._read(self.cfg.base_file)

    def mode_text(self, room_id: str) -> str:
        """当前群是否处于某个临时人格模式。"""
        name = self.memory.get_mode(room_id)
        if not name:
            return ""
        from .modes import mode_file

        path = mode_file(getattr(self.cfg, "mode_dir", "personas/modes"), name)
        return self._read(str(path)) if path else ""

    def group(self, room_id: str) -> str:
        path = self.cfg.group_files.get(room_id, "")
        return self._read(path) if path else ""

    def state(self) -> str:
        if not self.cfg.temporary_state:
            return ""
        return self._read(self.cfg.state_prompt)

    # ---- 心情 ----
    def mood(self, room_id: str, mood_cfg=None) -> int:
        value, updated_at = self.memory.get_mood_state(room_id, 50)
        if mood_cfg is not None and mood_cfg.enabled and value != 50:
            hours = max(0.0, (time.time() - updated_at) / 3600.0)
            drift = int(hours * mood_cfg.decay_per_hour)
            if drift:
                value = value - drift if value > 50 else value + drift
                if abs(value - 50) <= drift:
                    value = 50
                self.memory.set_mood(room_id, value)
        return value

    def decay(self, room_id: str, mood_cfg) -> int:
        current = self.mood(room_id)
        row_time = None
        # 用 updated_at 估算衰减：简单起见按每次访问衰减一点
        if current > 50:
            current = max(50, current - mood_cfg.decay_per_hour // 12)
        elif current < 50:
            current = min(50, current + mood_cfg.decay_per_hour // 12)
        self.memory.set_mood(room_id, current)
        return current

    def adjust_mood(self, room_id: str, text: str, mood_cfg) -> int:
        if not mood_cfg.enabled:
            return self.mood(room_id)
        delta = 0
        if any(word in text for word in NEGATIVE_WORDS):
            delta += mood_cfg.insult_delta
        if any(word in text for word in POSITIVE_WORDS):
            delta += mood_cfg.praise_delta
        current = self.mood(room_id)
        if delta:
            current = max(0, min(100, current + delta))
            self.memory.set_mood(room_id, current)
        return current

    def mood_band(self, value: int) -> str:
        for low, high, text in MOOD_BANDS:
            if low <= value < high:
                return text
        return MOOD_BANDS[2][2]

    # ---- 组装 ----
    def system_prompt(self, room_id: str, mood_cfg) -> str:
        parts = [self.base()]
        mode = self.mode_text(room_id)
        if mode:
            parts.append(f"\n\n【当前模式】\n{mode}")
        group = self.group(room_id)
        if group:
            parts.append(f"\n\n【本群设定】\n{group}")
        memory = getattr(self.memory, "get_group_memory", None)
        if callable(memory):
            record = memory(room_id)
            text = str(record.get("text", "")).strip()
            if text:
                parts.append(f"\n\n【本群记忆】\n{text}")
        state = self.state()
        if state:
            parts.append(f"\n\n【当前状态】\n{state}")
        parts.append(f"\n\n【语气】\n{TONE_HINTS.get(int(self.cfg.tone), TONE_HINTS[3])}")
        if mood_cfg.enabled:
            value = self.mood(room_id, mood_cfg)
            parts.append(f"\n\n【心情值 {value}/100】\n{self.mood_band(value)}")
        parts.append(
            "\n\n【硬规则】\n"
            "只输出要说的话，不要解释。不使用颜文字和 emoji。"
            "不确定的事实不要编。"
        )
        return "".join(parts)

    @staticmethod
    def now() -> float:
        return time.time()
