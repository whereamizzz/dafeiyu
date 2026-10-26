"""联网搜索：SearXNG 自建聚合 / Tavily API，带内存缓存。"""

from __future__ import annotations

import time

import httpx
from loguru import logger


class SearchClient:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._cache: dict[str, tuple[float, list[dict]]] = {}

    def _cached(self, query: str) -> list[dict] | None:
        hit = self._cache.get(query)
        if not hit:
            return None
        expires, value = hit
        if expires < time.time():
            self._cache.pop(query, None)
            return None
        return value

    async def search(self, query: str) -> list[dict]:
        if not self.cfg.enabled:
            return []
        cached = self._cached(query)
        if cached is not None:
            return cached
        try:
            if self.cfg.provider == "tavily":
                results = await self._tavily(query)
            else:
                results = await self._searxng(query)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"搜索失败: {exc}")
            return []
        results = [item for item in results if item.get("title") or item.get("content")][
            : self.cfg.max_results
        ]
        self._cache[query] = (time.time() + self.cfg.cache_ttl, results)
        return results

    async def _searxng(self, query: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
            response = await client.get(
                self.cfg.base_url,
                params={"q": query, "format": "json", "language": "zh-CN"},
            )
            response.raise_for_status()
            data = response.json()
        return [
            {
                "title": str(item.get("title", "")),
                "content": str(item.get("content", "")),
                "url": str(item.get("url", "")),
            }
            for item in (data.get("results") or [])
        ]

    async def _tavily(self, query: str) -> list[dict]:
        async with httpx.AsyncClient(timeout=self.cfg.timeout) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": self.cfg.api_key,
                    "query": query,
                    "max_results": self.cfg.max_results,
                    "search_depth": "basic",
                },
            )
            response.raise_for_status()
            data = response.json()
        return [
            {
                "title": str(item.get("title", "")),
                "content": str(item.get("content", "")),
                "url": str(item.get("url", "")),
            }
            for item in (data.get("results") or [])
        ]

    @staticmethod
    def format_results(results: list[dict]) -> str:
        if not results:
            return ""
        lines = []
        for index, item in enumerate(results, start=1):
            title = item.get("title") or "无标题"
            content = (item.get("content") or "").strip().replace("\n", " ")
            lines.append(f"{index}. {title}：{content[:220]}")
        return "\n".join(lines)
