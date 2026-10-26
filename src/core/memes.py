"""表情包库：按分类挑图，带冷却与每日上限。

素材来自之前下载的三套包（大肥鱼 / 爱弥斯 / 菲比啾比），
按 manifest 的分类整理到 data/memes/<pack>/<category>/。

策略（参考鲸鱼娘 4.0）：
- 先看这句话像什么场景（生气、开心、困惑、聊股票、聊原神…），选对应分类；
- 没命中就按心情随机挑一个通用分类；
- 同一张图 24 小时内不重复发；
- 有概率、有冷却、有每日上限，防止刷图。
"""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

from loguru import logger

CATEGORY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("气", "烦", "滚", "闭嘴", "无语", "傻", "骂"), "angry"),
    (("哈哈", "笑", "开心", "好耶", "棒", "牛", "厉害", "赢了"), "happy"),
    (("?", "？", "啥", "不懂", "疑惑", "为什么", "怎么"), "confused"),
    (("累了", "困", "睡", "摸鱼", "摆烂", "不想"), "sleep"),
    (("谢谢", "多谢", "感谢", "帮大忙"), "like"),
    (("股票", "基金", "A股", "美股", "涨", "跌", "亏"), "_stock"),
    (("原神", "米哈游", "抽卡"), "_genshin"),
    (("鸣潮", "库洛", "库狗"), "_kg"),
    (("钱", "token", "余额", "充值", "氪"), "givemoney"),
    (("工作", "加班", "报告", "任务", "deadline"), "work"),
    (("cpu", "烧了", "卡了", "转不动"), "cpu"),
    (("可爱", "喜欢", "抱", "贴贴"), "like"),
    (("惊", "居然", "没想到", "卧槽"), "surprised"),
]

GENERAL_CATEGORIES = ("happy", "confused", "sigh", "surprised", "like", "fool")


class MemeLibrary:
    def __init__(self, cfg, root: str | Path = "data/memes") -> None:
        self.cfg = cfg
        self.root = Path(root)
        self.index: dict = {}
        self._sent: dict[str, float] = {}
        self._last_send = 0.0
        self._daily = 0
        self._day = ""
        self._load()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.index)

    def _load(self) -> None:
        path = self.root / "index.json"
        if not path.exists():
            logger.warning("表情包索引不存在，表情功能关闭")
            return
        try:
            self.index = json.loads(path.read_text(encoding="utf-8-sig"))
            logger.info(
                f"表情包库已加载：{len(self.index.get('packs', []))} 套 / "
                f"{len(self.index.get('categories', {}))} 类"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"表情包索引读取失败: {exc}")

    def _categories(self) -> list[str]:
        categories = list((self.index.get("categories") or {}).keys())
        if self.cfg.packs:
            allowed = set(self.cfg.packs)
            categories = [
                cat
                for cat, meta in (self.index.get("categories") or {}).items()
                if allowed & set(meta.get("packs", []))
            ]
        return categories

    def _files(self, category: str) -> list[str]:
        files: list[str] = []
        packs = (self.index.get("categories") or {}).get(category, {}).get("packs", [])
        for pack in packs:
            folder = self.root / pack / category
            if folder.exists():
                files.extend(str(item) for item in folder.iterdir() if item.is_file())
        return files

    def pick(self, text: str, mood: int) -> str | None:
        if not self.enabled:
            return None
        now = time.time()
        if now - self._last_send < self.cfg.cooldown_seconds:
            return None
        day = time.strftime("%Y-%m-%d")
        if day != self._day:
            self._day = day
            self._daily = 0
        if self._daily >= self.cfg.daily_limit:
            return None
        if random.random() > self.cfg.probability:
            return None

        categories = self._categories()
        chosen = ""
        for keywords, category in CATEGORY_RULES:
            if any(word in text for word in keywords) and category in categories:
                chosen = category
                break
        if not chosen:
            pool = [cat for cat in GENERAL_CATEGORIES if cat in categories]
            chosen = random.choice(pool) if pool else (random.choice(categories) if categories else "")

        files = self._files(chosen) if chosen else []
        if not files:
            return None
        files = [item for item in files if now - self._sent.get(item, 0) > 86400]
        if not files:
            return None
        path = random.choice(files)
        self._sent[path] = now
        self._last_send = now
        self._daily += 1
        logger.info(f"发表情包（{chosen}）: {path}")
        return path
