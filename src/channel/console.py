"""控制台通道：不接微信时用来调试判定与人格。

控制台模式下没有真实的微信消息，测试入口走 Web 的 /api/simulate。
"""

from __future__ import annotations

from loguru import logger


class ConsoleChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def connect(self) -> bool:
        logger.info("控制台通道就绪（不会真的发微信）")
        return True

    async def send(self, room_id: str, text: str) -> bool:
        self.sent.append((room_id, text))
        logger.info(f"[模拟发送 -> {room_id}] {text}")
        return True

    async def send_image(self, room_id: str, path: str) -> bool:
        self.sent.append((room_id, f"[图片] {path}"))
        logger.info(f"[模拟发图 -> {room_id}] {path}")
        return True

    async def listen(self, callback) -> None:
        """控制台模式不监听真实消息，靠 /api/simulate 注入。"""
        logger.info("控制台模式：用控制台的「模拟消息」注入，或调 /api/simulate")
        while True:
            import asyncio

            await asyncio.sleep(3600)

    async def stop(self) -> None:
        return None
