"""门禁：把「回不回」之后的所有限制收在一处。

两条主通道，账本分开记：

- direct：@ / 点名 / 引用 / 私聊。只受「1 秒防重发」和「每小时硬上限」约束，
  不占主动插话的条数额度——被叫到不该被听到别人的额度挡住。
- interject：主动插话。受「说完等几条」「N 分钟几次」「N 分钟几条」「每小时上限」约束。

额外出站（表情包、画画）用 kind="extra" 记账：只计小时额度与最后发送时间，
既不算插话、也不重置「说了几句」。

之前这些检查散在 policy 和 pipeline 两处，改一处忘一处；
现在统一成 Gate.check()，返回 (是否放行, 原因, 需要等待的秒数)。
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class Gate:
    def __init__(self, policy_cfg, budget_cfg) -> None:
        self.cfg = policy_cfg
        self.budget = budget_cfg
        self._hourly: dict[str, deque] = defaultdict(deque)
        self._last_send: dict[str, float] = {}
        self._reply_window: dict[str, deque] = defaultdict(deque)
        self._interject_times: dict[str, list] = defaultdict(list)
        self._msgs_since_reply: dict[str, int] = defaultdict(int)

    # ---- 计数 ----
    def on_message(self, room_id: str) -> None:
        self._msgs_since_reply[room_id] += 1

    def on_reply(self, room_id: str, kind: str = "interject") -> None:
        """登记一次成功发出的消息。

        kind: direct=点名/私聊回；interject=主动插话；extra=表情包/画画等附加出站。
        """
        now = time.time()
        self._last_send[room_id] = now
        self._hourly[room_id].append(now)
        if kind == "interject":
            self._reply_window[room_id].append(now)
            self._interject_times[room_id].append(now)
            self._msgs_since_reply[room_id] = 0
        elif kind == "direct":
            self._msgs_since_reply[room_id] = 0

    def mark_interject(self, room_id: str) -> None:
        """兼容旧调用：只补记插话时间，不重复记发送。"""
        self._interject_times[room_id].append(time.time())

    # ---- 检查 ----
    def check(self, room_id: str, direct: bool, private: bool = False) -> tuple[bool, str, float]:
        now = time.time()
        hourly = self._hourly[room_id]
        while hourly and now - hourly[0] > 3600:
            hourly.popleft()
        if len(hourly) >= int(self.budget.per_hour):
            return False, "本小时额度已用完", 0.0

        last = self._last_send.get(room_id, 0.0)

        if direct:
            # 点名/私聊只防手抖连发，不参与插话配额
            if private:
                cooldown = max(0.5, int(self.cfg.private_cooldown_ms) / 1000.0)
            else:
                cooldown = 1.0
            if now - last < cooldown:
                wait = cooldown - (now - last)
                return False, f"距上次发送 {now - last:.1f}s，冷却中", wait
            return True, "", 0.0

        waited = self._msgs_since_reply[room_id]
        if waited < int(self.cfg.min_messages_between_interjections):
            return False, f"刚说过话，只等了 {waited} 条", 0.0

        window_seconds = int(self.cfg.interject_window_seconds)
        recent = [
            item
            for item in self._interject_times[room_id]
            if now - item < window_seconds
        ]
        self._interject_times[room_id] = recent
        if len(recent) >= int(self.cfg.interject_limit):
            return False, f"{window_seconds} 秒内插话次数已满", 0.0

        window = self._reply_window[room_id]
        while window and now - window[0] > int(self.cfg.reply_window_seconds):
            window.popleft()
        if len(window) >= int(self.cfg.reply_window_limit):
            return False, f"{self.cfg.reply_window_seconds} 秒内已插话 {len(window)} 条", 0.0

        gap = max(1.0, float(self.budget.min_gap_seconds))
        if now - last < gap:
            return False, f"距上次发送 {now - last:.1f}s", gap - (now - last)

        return True, "", 0.0
