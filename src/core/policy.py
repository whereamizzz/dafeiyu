"""接话决策：硬触发 + 模型打分 + 预算/冷却。"""

from __future__ import annotations

import re
import time
from collections import defaultdict, deque

from loguru import logger

from .decision.chain import DecisionChain, ExternalProvider, LLMProvider, RuleProvider, Signals
from .memory import Memory
from .provider import Provider

def cfg_seconds(cfg) -> int:
    return int(getattr(cfg, "reply_window_seconds", 600))


JUDGE_SYSTEM = (
    "你是群聊里的普通群友，判断自己要不要接下面这条消息。\n"
    "硬规则：命中任意一条就给 0 分——\n"
    "1. 消息 @ 的是别人；\n"
    "2. 明显在跟另一个具体的人说话（点名别人、引用别人的话）；但提到你的名字或外号不算；\n"
    "3. 纯表情、刷屏、系统消息、红包口令；\n"
    "4. 你接不上，只能尬回。\n"
    "其余按 0-10 打分：7 分以上表示你能自然接住或帮上忙。\n"
    "只输出：分数|一句不超过 15 字的理由。例如：8|在聊游戏，能接梗"
)


class Budget:
    """每小时上限 + 单群并发 + 最小间隔。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._hour: deque[float] = deque()
        self._last_send: dict[str, float] = {}
        self._busy: dict[str, int] = defaultdict(int)

    def _trim(self, now: float) -> None:
        while self._hour and now - self._hour[0] > 3600:
            self._hour.popleft()

    def allow(self, room_id: str) -> tuple[bool, str]:
        now = time.time()
        self._trim(now)
        if len(self._hour) >= self.cfg.per_hour:
            return False, "本小时额度已用完"
        if self._busy.get(room_id, 0) >= self.cfg.per_group_concurrent:
            return False, "该群并发已满"
        last = self._last_send.get(room_id, 0.0)
        if now - last < self.cfg.min_gap_seconds:
            return False, "距上次发送太近"
        return True, ""

    def note(self, room_id: str) -> None:
        now = time.time()
        self._hour.append(now)
        self._last_send[room_id] = now

    def enter(self, room_id: str) -> None:
        self._busy[room_id] += 1

    def leave(self, room_id: str) -> None:
        self._busy[room_id] = max(0, self._busy.get(room_id, 1) - 1)


class Policy:
    def __init__(
        self,
        cfg,
        budget_cfg,
        provider: Provider,
        memory: Memory,
        external_cfg=None,
        aliases: list[str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.provider = provider
        self.memory = memory
        self.budget = Budget(budget_cfg)
        self._recent_replies: dict[str, deque] = defaultdict(deque)
        self.aliases = list(aliases or ["大肥鱼", "肥鱼", "鲸鱼娘", "小鲸鱼", "小鱼"])
        self.last_defer = 0.0
        self.last_probability = 0.0
        self.self_wxid = ""
        self._peers: list[str] = []
        self.chain = DecisionChain(
            RuleProvider(cfg, peers_provider=lambda: self._peers),
            LLMProvider(provider, cfg, JUDGE_SYSTEM),
            ExternalProvider(external_cfg) if external_cfg is not None else None,
            threshold=0.65,
        )
        self._last_reply: dict[str, float] = {}

    def hard_trigger(self, content: str, is_at: bool) -> bool:
        if is_at:
            return True
        text = content.casefold()
        return any(trigger.casefold() in text for trigger in self.cfg.triggers)
        # 外号在 Signals.named 里统一判断

    async def decide(
        self,
        message: dict,
        is_at: bool = False,
        threshold_bonus: int = 0,
        context: list[dict] | None = None,
        since_last: float = 9999.0,
        consecutive: int = 0,
    ) -> tuple[bool, int, str]:
        content = str(message.get("content", ""))
        if self.hard_trigger(content, is_at) or not message.get("is_group", True):
            return True, 10, "必应"

        if not self.cfg.judge_enabled:
            return False, 0, "未开启插话判断"

        room_id = str(message.get("room_id", ""))
        cache_key = content.strip()[:60]
        cached = self.memory.get_judge_cache(room_id, cache_key)
        if cached is not None:
            self.last_probability = cached["probability"]
            self.last_defer = 0.0
            return cached["accepted"], cached["score"], cached["reason"]
        history = (
            context
            if context is not None
            else self.memory.recent_messages(room_id, 12)
        )
        threshold = max(1, self.cfg.score_threshold - max(0, threshold_bonus))
        self.chain.threshold = max(0.2, threshold / 10.0)

        signals = Signals(
            message,
            history,
            tuple(self.aliases),
            since_last,
            consecutive,
            self_wxid=getattr(self, "self_wxid", ""),
        )
        if self.cfg.observation:
            result = self.chain.rule.decide(signals)
        else:
            result = await self.chain.decide(signals)
        probability = float(result.get("probability", 0) or 0)
        score = int(round(probability * 10))
        reason = str(result.get("reason", ""))[:30]
        accepted = bool(result.get("reply"))
        try:
            self.memory.set_judge_cache(
                room_id, cache_key, score, reason, probability, accepted
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"判定缓存写入失败: {exc}")
        self.last_defer = float(result.get("defer") or 0.0)
        self.last_probability = probability
        try:
            self._peers = [
                str(item.get("nickname") or item.get("user_id"))
                for item in self.memory.list_contacts()
            ]
        except Exception:  # noqa: BLE001
            pass
        if accepted:
            self._last_reply[room_id] = time.time()
        return accepted, score, reason
