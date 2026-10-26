"""作息：静默时段与活跃时段。

设计意图：
- 静默时段（默认 00:00-08:00）不主动发言、不画图、非 @ 消息也不接，省 token；
- 活跃时段（默认 09:00-23:00）才允许主动行为；
- 时段可跨天（如 22:00-07:00）。
"""

from __future__ import annotations

from datetime import datetime, time as dtime


def _parse(value: str, default: dtime) -> dtime:
    try:
        hour, minute = str(value).split(":")[:2]
        return dtime(int(hour), int(minute))
    except Exception:  # noqa: BLE001
        return default


class Schedule:
    def __init__(self, cfg) -> None:
        self.enabled = bool(getattr(cfg, "enabled", True))
        self.active_bonus = int(getattr(cfg, "active_bonus", 2))
        self.quiet_allow_at = bool(getattr(cfg, "quiet_allow_at", False))
        self.quiet_start = _parse(cfg.quiet_start, dtime(0, 0))
        self.quiet_end = _parse(cfg.quiet_end, dtime(8, 0))
        self.active_start = _parse(cfg.active_start, dtime(9, 0))
        self.active_end = _parse(cfg.active_end, dtime(23, 0))

    @staticmethod
    def _in_window(now: dtime, start: dtime, end: dtime) -> bool:
        if start <= end:
            return start <= now < end
        return now >= start or now < end

    def is_quiet(self, moment: datetime | None = None) -> bool:
        if not self.enabled:
            return False
        now = (moment or datetime.now()).time()
        return self._in_window(now, self.quiet_start, self.quiet_end)

    def is_active(self, moment: datetime | None = None) -> bool:
        if not self.enabled:
            # 作息没开 = 全天都算活跃，否则主动行为永远不会触发
            return True
        now = (moment or datetime.now()).time()
        if self.is_quiet(moment):
            return False
        return self._in_window(now, self.active_start, self.active_end)

    def describe(self) -> str:
        return (
            f"静默 {self.quiet_start.strftime('%H:%M')}-{self.quiet_end.strftime('%H:%M')}，"
            f"活跃 {self.active_start.strftime('%H:%M')}-{self.active_end.strftime('%H:%M')}"
        )
