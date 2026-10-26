"""模型调用（OpenAI 兼容接口），带重试与超时。"""

from __future__ import annotations

import asyncio

import httpx
from loguru import logger


class Provider:
    def __init__(self, cfg) -> None:
        self.base_url = cfg.base_url
        self.api_key = cfg.api_key
        self.model = cfg.model
        self.temperature = cfg.temperature
        self.max_tokens = cfg.max_tokens
        self.timeout = cfg.timeout
        self.reasoning_effort = getattr(cfg, "reasoning_effort", "")
        self.disable_thinking = bool(getattr(cfg, "disable_thinking", True))
        self.reasoning_headroom = int(getattr(cfg, "reasoning_headroom", 200))
        self.warn_reasoning_ratio = float(getattr(cfg, "warn_reasoning_ratio", 0.5))
        self.last_reasoning_ratio = 0.0
        self.usage = {"prompt": 0, "completion": 0, "calls": 0}
        self.on_usage = None

    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.model and self.api_key)

    async def chat(
        self,
        messages: list[dict],
        system: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        if not self.ready:
            raise RuntimeError("模型未配置（需要 base_url / api_key / model）")

        payload: dict = {
            "model": self.model,
            "messages": ([{"role": "system", "content": system}] if system else []) + messages,
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": (
                (self.max_tokens if max_tokens is None else max_tokens)
                + (0 if self.disable_thinking else self.reasoning_headroom)
            ),
            "stream": False,
        }
        if self.disable_thinking:
            payload["thinking"] = {"type": "disabled"}
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
                    response = await client.post(
                        self.base_url, json=payload, headers=headers
                    )
                    response.raise_for_status()
                    data = response.json()
                usage = data.get("usage") or {}
                prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
                completion_tokens = int(usage.get("completion_tokens", 0) or 0)
                self.usage["prompt"] += prompt_tokens
                self.usage["completion"] += completion_tokens
                self.usage["calls"] += 1
                details = usage.get("completion_tokens_details") or {}
                reasoning = int(details.get("reasoning_tokens", 0) or 0)
                total_out = int(usage.get("completion_tokens", 0) or 0)
                self.last_reasoning_ratio = (
                    reasoning / total_out if total_out else 0.0
                )
                if reasoning and self.last_reasoning_ratio > self.warn_reasoning_ratio:
                    logger.warning(
                        f"思考占了 {self.last_reasoning_ratio:.0%} 的输出 token，"
                        f"考虑关思考或换模型"
                    )
                if self.on_usage:
                    try:
                        self.on_usage(prompt_tokens, completion_tokens)
                    except Exception:  # noqa: BLE001
                        pass
                return str(data["choices"][0]["message"]["content"] or "").strip()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning(f"模型调用失败（第 {attempt + 1} 次）: {exc}")
                await asyncio.sleep(1.0 + attempt)
        raise RuntimeError(f"模型调用失败: {last_error}")

    async def complete_json(self, prompt: str, system: str = "", max_tokens: int = 64) -> str:
        return await self.chat(
            [{"role": "user", "content": prompt}],
            system=system,
            temperature=0.0,
            max_tokens=max_tokens,
        )
