"""启动入口：装配各模块，启动微信通道与 Web 控制台。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import uvicorn
from loguru import logger

from src.core.config import load_config
from src.core.memory import Memory
from src.core.persona import Persona
from src.core.pipeline import Bot
from src.core.policy import Policy
from src.core.provider import Provider
from src.core.draw import Drawer
from src.core.schedule import Schedule
from src.core.search import SearchClient

ROOT = Path(__file__).resolve().parent


logger.add(
    ROOT / "logs" / "bot.log",
    encoding="utf-8",
    rotation="10 MB",
    retention=5,
    enqueue=True,
)


def _pid_alive(pid: int) -> bool:
    """判断进程还活着。Windows 上 os.kill(pid, 0) 会直接杀进程，不能用。"""
    import ctypes

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == 259
        return True
    finally:
        kernel32.CloseHandle(handle)


def acquire_lock():
    """单实例锁：同一时间只允许一个机器人进程。"""
    lock = ROOT / "data" / "bot.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists():
        try:
            pid = int(lock.read_text(encoding="utf-8").strip() or 0)
        except Exception:  # noqa: BLE001
            pid = 0
        if pid and _pid_alive(pid):
            logger.error(f"已有实例在运行（PID {pid}），本进程退出")
            return False
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return True


async def heartbeat_loop() -> None:
    """写心跳文件，给外部看门狗判断事件循环是否还活着。

    同时看一眼 stop.flag：窗口关了、看门狗也被收走了，后端不能独自留着
    继续抢鼠标找微信。
    """
    path = ROOT / "data" / "heartbeat.json"
    stop = ROOT / "data" / "stop.flag"
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            if stop.exists():
                logger.info("收到 stop.flag，后端主动退出")
                os._exit(0)
            path.write_text(
                json.dumps({"pid": os.getpid(), "ppid": os.getppid(), "ts": time.time()}),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(5)


def build(config):
    memory = Memory(config.db_path)
    provider = Provider(config.provider)
    provider.on_usage = memory.add_usage
    search = SearchClient(config.search)
    persona = Persona(config.persona, memory)
    policy = Policy(
        config.policy,
        config.budget,
        provider,
        memory,
        external_cfg=config.decision_external,
        aliases=config.persona.aliases,
    )
    schedule = Schedule(config.schedule)
    drawer = Drawer(config.draw)

    if config.wechat.backend == "console":
        from src.channel.console import ConsoleChannel

        channel = ConsoleChannel()
    else:
        from src.channel.wechat import WeChatChannel

        channel = WeChatChannel(config.wechat)

    policy.self_wxid = getattr(channel, "self_wxid", "")
    if hasattr(channel, "_asr_cfg"):
        channel._asr_cfg = config.asr
    if hasattr(channel, "_vision_cfg"):
        channel._vision_cfg = config.vision
    bot = Bot(
        config, memory, provider, search, persona, policy, channel,
        schedule=schedule, drawer=drawer,
    )
    return bot, memory, channel


async def main() -> None:
    if not acquire_lock():
        return

    asyncio.create_task(heartbeat_loop())

    config = load_config(ROOT / "config.yaml")
    bot, memory, channel = build(config)

    logger.info(f"机器人「{config.name}」启动，模型 {config.provider.model}")
    if not config.provider.api_key:
        logger.warning("未配置 API Key，判定和回复都会失败")

    if config.web.enabled:
        from src.web.app import create_app

        app = create_app(config, bot, memory)
        server = uvicorn.Server(
            uvicorn.Config(app, host=config.web.host, port=config.web.port, log_level="warning")
        )
        asyncio.create_task(server.serve())
        logger.info(f"控制台：http://{config.web.host}:{config.web.port}")

    if not await channel.connect():
        logger.error("通道连接失败，退出")
        return

    bot.purge_old()
    await bot.resume_pending()
    asyncio.create_task(bot.draw_loop())
    asyncio.create_task(bot.proactive_loop())
    asyncio.create_task(bot.watchdog_loop())
    asyncio.create_task(bot.outbox_loop())

    try:
        await channel.listen(bot.handle)
    except KeyboardInterrupt:
        pass
    finally:
        await channel.stop()
        memory.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
