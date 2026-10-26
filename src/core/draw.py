"""画画：本地简笔画（PIL）与云绘图 API，可多选。

- pil：零成本，画鲸鱼娘简笔涂鸦；
- openai：OpenAI 兼容的 /images/generations（硅基流动、通义、OpenAI 等都有）；
- dashscope：阿里云百炼「通义万相」，异步任务。
"""

from __future__ import annotations

import base64
import random
import time
from pathlib import Path

import httpx
from loguru import logger


class Drawer:
    def __init__(self, cfg, out_dir: str | Path = "data/drawings") -> None:
        self.cfg = cfg
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return bool(
            self.cfg.enabled and (self.cfg.pil or self.cfg.openai or self.cfg.dashscope)
        )

    async def draw(self, prompt: str) -> Path | None:
        """按勾选的路线依次尝试，失败自动换下一条。"""
        routes = []
        if self.cfg.pil:
            routes.append(self._pil)
        if self.cfg.openai:
            routes.append(self._openai)
        if self.cfg.dashscope:
            routes.append(self._dashscope)
        for route in routes:
            try:
                path = await route(prompt)
                if path:
                    return path
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"绘图路线失败（{route.__name__}）: {exc}")
        return None

    # ---- PIL 简笔画 ----
    async def _pil(self, prompt: str) -> Path | None:
        from PIL import Image, ImageDraw

        size = 480
        image = Image.new("RGB", (size, size), (246, 248, 252))
        draw = ImageDraw.Draw(image)
        body = (110, 180, 240)
        accent = (255, 170, 190)

        # 简易鲸鱼娘：圆身 + 尾巴 + 眼睛 + 气泡
        draw.ellipse((120, 170, 360, 360), fill=body)
        draw.polygon([(120, 250), (60, 210), (60, 290)], fill=body)
        draw.ellipse((190, 235, 220, 265), fill=(30, 40, 60))
        draw.ellipse((285, 235, 315, 265), fill=(30, 40, 60))
        draw.arc((215, 275, 285, 320), 20, 160, fill=(30, 40, 60), width=4)
        draw.ellipse((150, 285, 185, 310), fill=accent)
        draw.ellipse((315, 285, 350, 310), fill=accent)
        for index in range(3):
            r = 12 + index * 6
            x = 370 + index * 18
            y = 300 - index * 24
            draw.ellipse((x, y, x + r, y + r), outline=(150, 190, 230), width=2)

        path = self.out_dir / f"doodle-{int(time.time())}-{random.randint(100, 999)}.png"
        image.save(path)
        return path

    # ---- OpenAI 兼容 images ----
    async def _openai(self, prompt: str) -> Path | None:
        if not (self.cfg.api_url and self.cfg.api_key):
            return None
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                self.cfg.api_url,
                headers={"Authorization": f"Bearer {self.cfg.api_key}"},
                json={
                    "model": self.cfg.model or "gpt-image-1",
                    "prompt": f"{self.cfg.style_prompt} {prompt}".strip(),
                    "n": 1,
                    "size": "1024x1024",
                },
            )
            response.raise_for_status()
            data = response.json()
        item = (data.get("data") or [{}])[0]
        if item.get("b64_json"):
            raw = base64.b64decode(item["b64_json"])
            path = self.out_dir / f"cloud-{int(time.time())}.png"
            path.write_bytes(raw)
            return path
        url = item.get("url")
        if not url:
            return None
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(url)
            response.raise_for_status()
        path = self.out_dir / f"cloud-{int(time.time())}.png"
        path.write_bytes(response.content)
        return path

    # ---- 通义万相 ----
    async def _dashscope(self, prompt: str) -> Path | None:
        if not self.cfg.api_key:
            return None
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-Async": "enable",
        }
        payload = {
            "model": self.cfg.model or "wanx2.1-t2i-turbo",
            "input": {"prompt": f"{self.cfg.style_prompt} {prompt}".strip()},
            "parameters": {"size": "1024*1024", "n": 1},
        }
        async with httpx.AsyncClient(timeout=60) as client:
            created = await client.post(
                "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis",
                headers=headers,
                json=payload,
            )
            created.raise_for_status()
            task_id = created.json()["output"]["task_id"]

            for _ in range(30):
                await _sleep(2)
                status = await client.get(
                    f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}",
                    headers={"Authorization": f"Bearer {self.cfg.api_key}"},
                )
                status.raise_for_status()
                body = status.json()
                state = body["output"].get("task_status")
                if state == "SUCCEEDED":
                    url = body["output"]["results"][0]["url"]
                    image = await client.get(url)
                    image.raise_for_status()
                    path = self.out_dir / f"wanx-{int(time.time())}.png"
                    path.write_bytes(image.content)
                    return path
                if state == "FAILED":
                    return None
        return None


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
