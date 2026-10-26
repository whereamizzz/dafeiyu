"""接话决策：规则层 + 模型层 + 可选外部层，永远有兜底。

设计借鉴自鲸舱（WhalePod, MIT）的 decision 分层：
- rule：零成本、永远可用，先筛掉明显的；
- llm：只有规则判不准时才会被调到；
- external：预留的 HTTP 分类器接口（Jev 之类），默认关闭。

三个 provider 都返回统一结构：
    {"reply": bool, "probability": float, "reason": str, "decided": bool}
`decided=True` 表示规则已经掷过骰子、结论可直接采信。
"""

from __future__ import annotations

import random
import re
import time

import httpx
from loguru import logger

STOP_GRAMS = {
    "今天", "现在", "什么", "怎么", "这个", "那个", "就是", "还是",
    "可以", "没有", "知道", "觉得", "一个", "我们", "你们", "他们",
    "自己", "时候", "因为", "所以", "但是", "如果", "然后", "已经",
}

REFERENTIAL_OPENING = re.compile(
    r"^(那|所以|这个|那个|然后|接着|还有|另外|要不|要我说|你看|你说)"
)
QUESTION_TAIL = re.compile(
    r"(呢|吗|吧|么|啥|怎么|为什么|为啥|哪个|哪里|多少|咋)\s*[啊呀哦噢诶]?\s*[。.!！~～]?$"
)


class Signals:
    """把一条消息翻译成决策需要的信号。"""

    def __init__(self, message: dict, context: list[dict], self_names: tuple[str, ...], since_last: float, consecutive: int, self_wxid: str = "") -> None:
        self.message = message
        self.context = context
        self.text = str(message.get("content", "")).strip()
        self.sender = str(message.get("sender", ""))
        self.self_names = self_names
        self.since_last = since_last
        self.consecutive = consecutive
        self.self_wxid = self_wxid
        self.is_group = bool(message.get("is_group", False))
        self.is_private = not self.is_group

    @property
    def named(self) -> bool:
        return any(name and name in self.text for name in self.self_names)

    @property
    def is_question(self) -> bool:
        return bool(re.search(r"[?？]", self.text)) or bool(QUESTION_TAIL.search(self.text))

    @property
    def quoted_me(self) -> bool:
        return bool(self.message.get("quoted_from")) and self.message.get("quoted_from") == self.self_wxid

    @property
    def points_at_me(self) -> bool:
        return bool(re.search(r"(你|您|你们)", self.text)) or bool(REFERENTIAL_OPENING.match(self.text))

    @property
    def pure_noise(self) -> bool:
        stripped = re.sub(r"[\s，。！？!?~～、,.·]", "", self.text)
        return len(stripped) == 0 or bool(re.fullmatch(r"(哈|嘿|嗯|哦|噢|啊|额|呵)+", stripped))

    def peer_mentioned(self, peers: list[str]) -> bool:
        for name in peers:
            if len(name) >= 2 and name in self.text and name != self.sender:
                return True
        return False

    def topic_continues(self, window: int = 4) -> bool:
        """最近几条是不是围着同一个话题（简单版：共享 2 字片段）。"""
        recent = [str(item.get("content", "")) for item in self.context[-window - 1:-1]]
        if not recent:
            return False
        current = set(self._grams(self.text))
        for text in recent:
            if current & set(self._grams(str(text))):
                return True
        return False

    @staticmethod
    def _grams(text: str) -> list[str]:
        clean = re.sub(r"[\s\W]+", "", text)
        grams = [clean[index:index + 2] for index in range(len(clean) - 1)]
        return [item for item in grams if item not in STOP_GRAMS]


class RuleProvider:
    """零成本规则层：只做像人会做的事，不假装聪明。"""

    name = "rule"

    def __init__(self, cfg, peers_provider=None) -> None:
        self.cfg = cfg
        self.peers_provider = peers_provider

    def decide(self, signals: Signals) -> dict:
        if signals.quoted_me or signals.message.get("is_at"):
            return {"reply": True, "probability": 0.97, "reason": "被点名或引用", "decided": True}
        if signals.is_private:
            return {"reply": True, "probability": 0.95, "reason": "私聊", "decided": True}
        if signals.pure_noise:
            return {"reply": False, "probability": 0.02, "reason": "纯语气词", "decided": True}
        if signals.consecutive >= getattr(self.cfg, "max_consecutive_turns", 3):
            return {"reply": False, "probability": 0.03, "reason": "连续发言过多，先让别人说", "decided": True}

        peers = self.peers_provider() if self.peers_provider else []
        if signals.peer_mentioned(peers):
            return {"reply": False, "probability": 0.05, "reason": "在叫别的群友", "decided": True}

        if signals.named:
            return {"reply": True, "probability": 0.95, "reason": "提到名字", "decided": True}


        if signals.is_question and signals.points_at_me:
            return {"reply": True, "probability": 0.85, "reason": "在问我", "decided": True}

        if signals.topic_continues():
            return {"reply": True, "probability": 0.55, "reason": "接着同一个话题", "decided": True}

        return {"reply": False, "probability": 0.05, "reason": "没叫到，也没接上", "decided": True}


class LLMProvider:
    """模型层：规则判不准时的补充，返回 0-10 分换算成概率。"""

    name = "llm"

    def __init__(self, provider, cfg, judge_system: str) -> None:
        self.provider = provider
        self.cfg = cfg
        self.judge_system = judge_system

    async def decide(self, signals: Signals) -> dict:
        lines = [
            f"{item.get('sender', '')}：{str(item.get('content', ''))[:100]}"
            for item in signals.context[-10:]
        ]
        prompt = (
            "最近群聊：\n"
            + ("\n".join(lines) or "（暂无）")
            + f"\n\n新消息：{signals.text[:200]}"
        )
        try:
            answer = await self.provider.chat(
                [{"role": "user", "content": prompt}],
                system=self.judge_system,
                temperature=0.0,
                max_tokens=8,
                timeout=8.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"判分失败（模型没回），按规则兜底: {exc}")
            return {
                "reply": False,
                "probability": 0.0,
                "reason": "判分失败：模型没回",
                "decided": False,
                "failed": True,
            }
        match = re.search(r"(\d{1,2})\s*[|｜]\s*(.*)", str(answer))
        if not match:
            logger.warning(f"判分失败（输出无法解析），按规则兜底: {str(answer)[:60]}")
            return {
                "reply": False,
                "probability": 0.0,
                "reason": "判分失败：输出没分",
                "decided": False,
                "failed": True,
            }
        score = int(match.group(1))
        reason = match.group(2).strip()[:30]
        probability = max(0.0, min(1.0, score / 10))
        return {"reply": score >= 7, "probability": probability, "reason": reason, "decided": False}


class ExternalProvider:
    """预留：HTTP 分类器（Jev 等）。默认关闭，配置齐了才启用。"""

    name = "external"

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", False) and self.cfg.base_url and self.cfg.api_key)

    async def decide(self, signals: Signals) -> dict:
        payload = {
            "task": "should-reply",
            "context": {
                "scene": "group" if signals.is_group else "private",
                "sinceMyLastMessageMs": int(signals.since_last * 1000),
                "consecutiveTurns": signals.consecutive,
                "messages": [
                    {"sender": item.get("sender", ""), "text": str(item.get("content", ""))[:120]}
                    for item in signals.context[-12:]
                ],
            },
            "candidates": [
                {"id": "reply", "label": "现在应该发言"},
                {"id": "silent", "label": "现在不该发言"},
            ],
            "options": {"returnProbability": True},
        }
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"}
        async with httpx.AsyncClient(timeout=getattr(self.cfg, "timeout", 8)) as client:
            response = await client.post(self.cfg.base_url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        choice = data.get("choice") or data.get("decision")
        probability = data.get("probability") or data.get("confidence") or 0.0
        reply = choice in ("reply", True, "yes")
        return {"reply": bool(reply), "probability": float(probability), "reason": f"external:{choice}", "decided": False}


class DecisionChain:
    """按顺序尝试，规则永远收尾。"""

    def __init__(self, rule: RuleProvider, llm: LLMProvider | None, external: ExternalProvider | None, threshold: float = 0.65) -> None:
        self.rule = rule
        self.llm = llm
        self.external = external
        self.threshold = threshold
        self._rng = random.Random()

    async def decide(self, signals: Signals) -> dict:
        if self.external and self.external.enabled:
            try:
                result = await self.external.decide(signals)
                return self._finish(result)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"外部决策不可用，降级: {exc}")
        if self.llm is not None:
            try:
                result = await self.llm.decide(signals)
                if result.get("failed"):
                    logger.info("模型判分不可用，改用规则判定")
                else:
                    return self._finish(result)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"模型决策异常，降级到规则: {exc}")
        return self._finish(self.rule.decide(signals))

    def _finish(self, result: dict) -> dict:
        if result.get("decided"):
            return result
        probability = float(result.get("probability", 0))
        reply = probability >= self.threshold
        return {**result, "reply": reply}
