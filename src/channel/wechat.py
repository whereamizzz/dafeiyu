"""微信通道：直接读本地数据库轮询新消息（不截图、不抢鼠标）。

发送才走 UI 自动化；收消息完全走数据库，稳定且零干扰。
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable

from loguru import logger

from . import uia_sender
from ..core.identity import SenderResolver


def _time_now() -> float:
    import time

    return time.time()

MessageCallback = Callable[[dict], Awaitable[None]]

SELF_SENDER_IDS = (2,)


class WeChatChannel:
    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._wx = None
        self._db = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._callback: MessageCallback | None = None
        self._listening = False
        self._names: dict[str, str] = {}
        self._cursors: dict[str, int] = {}
        self._self_wxid = ""
        self._self_name = ""
        self._media = None
        self._send_lock = None
        self._recent_sent: dict[str, tuple[str, float]] = {}
        self._asr_cfg = getattr(cfg, "asr", None)
        self._vision_cfg = getattr(cfg, "vision", None)
        self._last_error = ""
        self._resolver: SenderResolver | None = None

    # ---- 连接 ----
    @staticmethod
    def _install_safe_paste() -> None:
        """把库里的“清空输入框”改成 SetValue：不发 Ctrl+A/Delete。

        原来 _paste_into 会按 Ctrl+A + Delete，焦点一旦不在微信就会
        全选并删除桌面图标。这里改为：
        - 用 ValuePattern 读当前内容，非空就用 SetValue("") 清空
        - 只发 Ctrl+V 粘贴
        全程不按 Delete。
        """
        try:
            from wechatauto.uia_driver import WeChatUIA as UiaDriver
        except Exception:  # noqa: BLE001
            return

        if getattr(UiaDriver, "_safe_paste_installed", False):
            return

        def _safe_paste(self, ctrl, text: str, clear: bool = True) -> None:
            self._click_ctrl(ctrl)
            time.sleep(0.1)
            if clear:
                # 只全选、不按 Delete：粘贴会替换选区。
                # 万一焦点在桌面，Ctrl+A 只是选中图标，Ctrl+V 无目标，不会删东西。
                try:
                    ctrl.SendKeys("{Ctrl}a{Backspace}", waitTime=0.05)
                except Exception:  # noqa: BLE001
                    pass
            self._clip_set(text)
            try:
                ctrl.SendKeys("{Ctrl}v", waitTime=0.05)
            except Exception:  # noqa: BLE001
                pass

        UiaDriver._paste_into = _safe_paste
        UiaDriver._safe_paste_installed = True
        logger.info("已启用安全粘贴：发送过程不再按 Delete")

    async def connect(self) -> bool:
        self._loop = asyncio.get_running_loop()
        try:
            from wechatauto import WeChat

            self._wx = await self._loop.run_in_executor(
                None, lambda: WeChat(start_listener=False)
            )
            self._db = getattr(self._wx, "_db", None)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"微信连接失败: {exc}")
            return False
        if self._db is None:
            logger.error("拿不到本地数据库句柄，收消息会失效")
            return False
        try:
            self._resolver = SenderResolver(self._db)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"发送者解析器初始化失败（人名可能不准）：{exc}")
            self._resolver = None
        try:
            try:
                from wechatauto.media import MediaDownloader

                self._media = MediaDownloader(self._db)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"媒体下载器不可用: {exc}")
            info = self._db.get_self_info()
            self._self_wxid = str(info.get("username", ""))
            self._self_name = str(info.get("nick_name") or info.get("nickname") or "")
            self._install_safe_paste()
            logger.info(f"微信已连接：{info.get('nickname', '')}（{self._self_wxid}）")
        except Exception:  # noqa: BLE001
            logger.info("微信已连接（未取到自身信息）")
        return True

    @property
    def self_wxid(self) -> str:
        return self._self_wxid

    @property
    def connected(self) -> bool:
        return self._db is not None

    @property
    def last_error(self) -> str:
        """最近一次发送失败的人话原因，给控制台和待发队列用。"""
        return self._last_error

    def _fail(self, reason: str) -> bool:
        self._last_error = reason
        logger.warning(f"发送失败：{reason}")
        return False

    @staticmethod
    def _weixin_pids() -> list[int]:
        try:
            import psutil
        except Exception:  # noqa: BLE001
            return []
        pids: list[int] = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if "weixin" in name or "wechat" in name:
                    pids.append(int(proc.info["pid"]))
            except Exception:  # noqa: BLE001
                continue
        return pids

    @staticmethod
    def _wechat_windows(pids: list[int]) -> list[tuple[str, str, int, int]]:
        """可见的微信顶层窗口 (类名, 标题, 宽, 高)。"""
        import win32gui
        import win32process

        found: list[tuple[str, str, int, int]] = []

        def _scan(hwnd, _param):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid not in pids:
                    return True
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                width, height = right - left, bottom - top
                if width < 120 or height < 120:
                    return True
                found.append(
                    (win32gui.GetClassName(hwnd), win32gui.GetWindowText(hwnd), width, height)
                )
            except Exception:  # noqa: BLE001
                pass
            return True

        win32gui.EnumWindows(_scan, None)
        return found

    @staticmethod
    def _wechat_db_age() -> int:
        """微信消息库最新写入距今多少秒；拿不到就返回 -1。"""
        from pathlib import Path
        import time

        root = Path.home() / "xwechat_files"
        try:
            newest = 0.0
            for pattern in ("*/db_storage/message/message_0.db", "*/db_storage/message/message_0.db-wal"):
                for path in root.glob(pattern):
                    try:
                        newest = max(newest, path.stat().st_mtime)
                    except OSError:
                        continue
            if newest <= 0:
                return -1
            return max(0, int(time.time() - newest))
        except Exception:  # noqa: BLE001
            return -1

    def wechat_state(self) -> dict:
        """给控制台看的微信健康状况：ok / login / no_window / down / uia_down。"""
        db_age = self._wechat_db_age()
        pids = self._weixin_pids()
        base = {"db_age": db_age, "pids": len(pids)}
        if not pids:
            return {**base, "state": "down", "detail": "微信进程没开，收不到也发不出"}
        try:
            windows = self._wechat_windows(pids)
        except Exception:  # noqa: BLE001
            windows = []
        if not windows:
            return {**base, "state": "no_window", "detail": "微信没有可见窗口，多半缩在托盘或掉线了"}
        login = [item for item in windows if "LoginWindow" in item[0] or "登录" in item[1]]
        if login:
            return {
                **base,
                "state": "login",
                "detail": "微信停在登录页（掉线或被风控），需要在大肥鱼的手机上扫码",
            }
        if not any("Qt" in item[0] or "WeChatMainWnd" in item[0] for item in windows):
            return {**base, "state": "no_window", "detail": "只找到微信的辅助窗口，主界面没出来"}
        if not self._uia_ready():
            return {
                **base,
                "state": "uia_down",
                "detail": "微信界面读不出来（UIA 不可用），发送会失败",
            }
        quiet = f"，消息库已 {db_age // 60} 分钟没写入" if db_age >= 600 else ""
        return {**base, "state": "ok", "detail": "正常" + quiet}

    # ---- 发送（UI 自动化） ----
    async def send(self, room_id: str, text: str) -> bool:
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        async with self._send_lock:
            return await self._send_locked(room_id, text)

    async def _send_locked(self, room_id: str, text: str) -> bool:
        if str(getattr(self.cfg, "send_backend", "uia")).lower() == "uia":
            if not self._foreground_is_wechat():
                try:
                    self._ensure_window()
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(0.3)
                if not self._foreground_is_wechat():
                    return self._fail("微信不在前台，切窗口也没切过来")
            target_name = self._names.get(room_id) or await self._resolve(room_id)
            if not await self._ensure_target_chat(target_name):
                return self._fail(f"打不开和「{target_name}」的聊天窗口，会话没过校验")
            ok = await self._loop.run_in_executor(
                None, uia_sender.send, target_name, text
            )
            if ok:
                self._recent_sent[room_id] = (text, __import__("time").time())
                return True
            return self._fail(f"UIA 发送没成功（{target_name}）")
        import time as _t

        now = _t.time()
        previous = self._recent_sent.get(room_id)
        if previous and previous[0] == text and now - previous[1] < 60:
            logger.warning(f"60 秒内已发过同样内容，跳过: {text[:24]}")
            return True
        if not self._wx:
            return self._fail("还没连上微信（数据库句柄是空的）")
        target = self._names.get(room_id) or await self._resolve(room_id)
        if not self._known_target(room_id, target):
            return self._fail(f"找不到目标会话（{room_id}）")
        import os as _os
        if _os.environ.get("BOT_TEST_MODE") == "1" and bool(
            getattr(self.cfg, "require_consent", False)
        ):
            from pathlib import Path as _Path

            if not (_Path(__file__).resolve().parents[2] / "data" / "CONSENT").exists():
                return self._fail("测试模式没有同意文件（data/CONSENT），没发")
        if not self._foreground_is_wechat():
            try:
                self._ensure_window()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.4)
            if not self._foreground_is_wechat():
                return self._fail("微信不在前台，为了不误按键没发")
        if not self._uia_ready():
            if bool(getattr(self.cfg, "require_uia", False)):
                return self._fail("微信界面读不出来（UIA 不可用），可能需要重新登录")
            logger.debug("UIA 不可用，走 OCR（前台与坐标已校验）")
        try:
            import win32gui

            if not self._rect_sane(win32gui.GetForegroundWindow()):
                return self._fail("微信窗口位置异常，怕点歪，没发")
        except Exception:  # noqa: BLE001
            pass
        last_error = ""
        started_at = _time_now()
        for attempt in range(1, 4):
            try:
                result = await asyncio.wait_for(
                    self._loop.run_in_executor(
                        None,
                        lambda: (
                            self._ensure_window(),
                            self._wx.SendMsg(text, who=target, exact=False),
                        )[1],
                    ),
                    timeout=self.cfg.send_timeout,
                )
                if result is not False:
                    self._recent_sent[room_id] = (text, _t.time())
                    return True
                last_error = "返回失败"
            except asyncio.TimeoutError:
                logger.warning(f"发送超时（第 {attempt} 次）: {target}")
                if await self._verify_sent(room_id, text, started_at):
                    logger.info("超时但数据库里已有这条，按成功处理")
                    self._recent_sent[room_id] = (text, _t.time())
                    return True
                if attempt >= 3:
                    return self._fail("发送超时，微信一直没反应，也没落库")
                logger.warning(f"超时且没落库，准备第 {attempt + 1} 次尝试")
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            logger.warning(f"发送失败（第 {attempt} 次，{last_error}）: {target}")
            if await self._verify_sent(room_id, text, started_at):
                logger.info("数据库里已有这条，按发送成功处理")
                return True
            await asyncio.sleep(1.0 * attempt)
        return self._fail(f"发给「{target}」最终失败：{last_error or '没拿到原因'}")

    async def send_image(self, room_id: str, path: str) -> bool:
        """发图：打开会话 → 把图片写进剪贴板 → Ctrl+V → 回车。

        wechatauto 没有 SendImage，SendFiles 会卡在文件选择上；
        粘贴是微信里最稳的图片发送方式。
        """
        if not self._wx:
            return False
        target = self._names.get(room_id) or await self._resolve(room_id)
        try:
            await asyncio.wait_for(
                self._loop.run_in_executor(
                    None, lambda: self._paste_image(target, path)
                ),
                timeout=25,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"发图失败: {exc}")
            return False

    @staticmethod
    def _clipboard_image(path: str) -> bool:
        import win32clipboard
        from io import BytesIO

        from PIL import Image

        image = Image.open(path).convert("RGB")
        output = BytesIO()
        image.save(output, "BMP")
        data = output.getvalue()[14:]
        output.close()
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
        finally:
            win32clipboard.CloseClipboard()
        return True

    def _paste_image(self, target: str, path: str) -> None:
        import time as _time

        import pyautogui

        self._wx.ChatWith(target)
        _time.sleep(0.6)
        self._clipboard_image(path)
        _time.sleep(0.2)
        pyautogui.hotkey("ctrl", "v")
        _time.sleep(1.0)
        pyautogui.press("enter")
        _time.sleep(0.6)

    async def _verify_sent(self, room_id: str, text: str, since: float) -> bool:
        """发完之后去数据库看这条到底有没有落库。"""
        needle = str(text).strip()[:18]
        if not needle:
            return False

        def check() -> bool:
            try:
                rows = self._db.get_new_messages(room_id, 0, 50)
            except Exception:  # noqa: BLE001
                return False
            for row in reversed(rows or []):
                try:
                    created = int(row.get("create_time", 0) or 0)
                except Exception:  # noqa: BLE001
                    created = 0
                if created and created < since - 2:
                    break
                if needle in str(row.get("content", "")):
                    return True
            return False

        for delay in (2.0, 4.0, 6.0):
            await asyncio.sleep(delay)
            if await self._loop.run_in_executor(None, check):
                return True
        return False

    async def _ensure_target_chat(self, target: str) -> bool:
        """用 UIA 确认当前会话就是要发的人；不是就打开并复核。"""
        if self._uia_ready() is False:
            return False
        try:
            gui = self._wx._gui
            uia = gui._get_uia()
            if uia is None:
                return False

            def current() -> str:
                try:
                    return str(uia.current_chat() or "")
                except Exception:  # noqa: BLE001
                    return ""

            def open_chat() -> bool:
                try:
                    return bool(uia.open_chat(target))
                except Exception:  # noqa: BLE001
                    return False

            if current() == target:
                return True
            if not await self._loop.run_in_executor(None, open_chat):
                return False
            await asyncio.sleep(0.5)
            return current() == target
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"会话校验失败: {exc}")
            return False

    def _uia_ready(self) -> bool:
        """UIA 是否可用；不可用就不让库退回 OCR 盲点。"""
        try:
            gui = getattr(self._wx, "_gui", None)
            if gui is None:
                return False
            getter = getattr(gui, "_get_uia", None)
            if not callable(getter):
                return False
            return getter() is not None
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _rect_sane(hwnd: int) -> bool:
        """窗口坐标是否正常；负三万那种垃圾值不点。"""
        try:
            import win32gui

            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width = right - left
            height = bottom - top
            if width < 200 or height < 150:
                return False
            if abs(left) > 10000 or abs(top) > 10000:
                return False
            return True
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _foreground_is_wechat() -> bool:
        """前台窗口是不是微信；不是就绝不发键盘，防止误操作落到桌面上。"""
        try:
            import psutil
            import win32gui
            import win32process

            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return False
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            name = psutil.Process(pid).name().lower()
            return "wechat" in name or "weixin" in name
        except Exception:  # noqa: BLE001
            return False

    def _known_target(self, room_id: str, target: str) -> bool:
        """发送前确认这个会话是真的存在，避免给未知会话发导致卡死。"""
        room = str(room_id)
        if room.endswith("@chatroom") or room.startswith("wxid_"):
            return str(target) != room or room in self._names
        return True

    def _window_ready(self) -> bool:
        """窗口是否可见；最小化时 UI 自动化会一直卡到超时。"""
        try:
            self._ensure_window()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _ensure_window(self) -> None:
        """把微信窗口拉回可见状态；最小化时 UI 自动化会全部阻塞。"""
        try:
            show = getattr(self._wx, "Show", None)
            if callable(show):
                show()
        except Exception:  # noqa: BLE001
            pass

    async def _resolve(self, room_id: str) -> str:
        if not (room_id.endswith("@chatroom") or room_id.startswith("wxid_")):
            return room_id
        try:
            if self._db is not None:
                name = await self._loop.run_in_executor(
                    None, lambda: self._db.get_nickname(room_id)
                )
                if name:
                    self._names[room_id] = str(name)
                    return str(name)
        except Exception:  # noqa: BLE001
            pass
        return room_id

    # ---- 收消息（数据库轮询） ----
    async def listen(self, callback: MessageCallback) -> None:
        self._callback = callback
        self._listening = True
        await self._prime_cursors()
        logger.info(f"数据库轮询已启动（每 {self.cfg.poll_interval}s）")
        while self._listening:
            try:
                await self._poll_once()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"轮询失败: {exc}")
            await asyncio.sleep(self.cfg.poll_interval)

    async def _prime_cursors(self) -> None:
        """启动时把游标推到最新，避免把历史消息当新消息回一遍。"""
        try:
            sessions = await self._loop.run_in_executor(None, self._db.get_sessions)
            for item in sessions or []:
                username = str(item.get("username", ""))
                if not username:
                    continue
                rows = await self._loop.run_in_executor(
                    None, lambda u=username: self._db.get_new_messages(u, 0, 200)
                )
                if rows:
                    self._cursors[username] = max(
                        int(row.get("sort_seq", 0) or 0) for row in rows
                    )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"初始化游标失败: {exc}")

    async def _poll_once(self) -> None:
        sessions = await self._loop.run_in_executor(None, self._db.get_sessions)
        for item in sessions or []:
            username = str(item.get("username", ""))
            if not username:
                continue
            cursor = self._cursors.get(username, 0)
            rows = await self._loop.run_in_executor(
                None, lambda u=username, c=cursor: self._db.get_new_messages(u, c, 30)
            )
            for row in rows or []:
                seq = int(row.get("sort_seq", 0) or 0)
                if seq <= cursor:
                    continue
                self._cursors[username] = seq
                try:
                    await self._emit(username, row)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"单条消息处理失败 type={row.get('type')} "
                        f"id={row.get('local_id')}: {exc}"
                    )

    @staticmethod
    def _strip_prefix(content: str, sender_wxid: str) -> str:
        """群消息在库里带「发送人:」前缀，去掉再进流水线。"""
        text = content.strip()
        if sender_wxid and text.startswith(sender_wxid + ":"):
            return text[len(sender_wxid) + 1:].strip()
        for marker in ("@chatroom:", "wxid_"):
            index = text.find(":\n")
            if index > 0 and marker in text[:index]:
                return text[index + 1:].strip()
        return text

    @staticmethod
    def _parse_quote(content: str) -> tuple[str, str]:
        """解析微信引用回复：返回 (被引用人的 wxid, 被引用内容)。"""
        if "<refermsg>" not in content:
            return "", ""
        block = content[content.find("<refermsg>"):]
        import re as _re
        from_user = _re.search(r"<fromusr>(.*?)</fromusr>", block)
        text = _re.search(r"<content>(.*?)</content>", block, _re.S)
        return (
            (from_user.group(1) if from_user else ""),
            (text.group(1)[:80] if text else ""),
        )

    @staticmethod
    def _strip_quote_xml(content: str) -> str:
        import re as _re
        text = _re.sub(r"<[^>]+>", " ", content)
        return _re.sub(r"\s+", " ", text).strip()

    def _capture_sticker(self, room_name: str) -> str:
        """截取该会话最新一条表情气泡，返回 PNG 路径；不适用就返回空串。"""
        from pathlib import Path

        if not room_name or getattr(self, "_wx", None) is None:
            return ""
        try:
            self._wx.ChatWith(room_name)
            time.sleep(0.4)
            messages = self._wx.GetAllMessage() or []
            if not messages:
                return ""
            latest = messages[0]
            if getattr(latest, "type", None) != "emotion":
                return ""
            save_dir = str(Path("data/stickers"))
            Path(save_dir).mkdir(parents=True, exist_ok=True)
            path = latest.capture(save_dir=save_dir)
            return str(path or "")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"表情截图失败: {exc}")
            return ""

    async def _describe_sticker(self, username: str, room_name: str) -> str:
        """表情包识图：视觉为主，失败就当没看清。"""
        cfg = self._vision_cfg
        if cfg is None or not getattr(cfg, "enabled", False):
            return ""
        if not getattr(self.cfg, "read_stickers", True):
            return ""
        if time.time() < getattr(self, "_vision_cooldown_until", 0):
            return ""
        room_name = room_name or self._names.get(username, "")
        if not room_name:
            return ""
        try:
            path = await self._loop.run_in_executor(
                None, lambda: self._capture_sticker(room_name)
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"表情截图异常: {exc}")
            return ""
        if not path:
            return ""
        from ..core.vision import describe

        try:
            text = await describe(path, cfg)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"表情识图失败: {exc}")
            self._vision_cooldown_until = time.time() + 600
            return ""
        return str(text or "").strip()[:60]

    async def _describe_image(self, username: str, local_id: int) -> str:
        if self._media is None or self._vision_cfg is None:
            return ""
        cfg = self._vision_cfg
        if not getattr(cfg, "enabled", False):
            return ""
        try:
            path = await self._loop.run_in_executor(
                None, lambda: self._media.download_image(username, local_id)
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"图片下载失败: {exc}")
            return ""
        if not path:
            return ""
        from ..core.vision import describe

        return await describe(path, cfg)

    async def _transcribe(self, username: str, local_id: int) -> str:
        if self._media is None:
            return ""
        cfg = self._asr_cfg
        if cfg is None or not getattr(cfg, "enabled", False):
            logger.warning("语音识别未启用或配置缺失，跳过转写")
            return ""
        try:
            path = await self._loop.run_in_executor(
                None, lambda: self._media.download_voice(username, local_id)
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"语音下载失败: {exc}")
            return ""
        if not path:
            return ""
        from ..core.asr import transcribe

        text = await self._loop.run_in_executor(None, transcribe, path, cfg)
        if not text:
            logger.warning(f"语音转写为空: {path}")
        return text

    async def _describe_card(self, raw: str) -> str:
        """把分享卡片的 XML 转成「标题 + 摘要 + 正文」。

        卡片消息在数据库里的类型是「文件/链接/卡片」，content 是一段
        <msg><appmsg>…</appmsg></msg> 的 XML，title/des/url 都在里面。
        原样丢给模型它只会看到一堆尖括号，所以这里先翻译成人话。
        """
        import html
        import re

        def pick(tag: str) -> str:
            match = re.search(rf"<{tag}>(.*?)</{tag}>", raw, re.S | re.I)
            return html.unescape(match.group(1)).strip() if match else ""

        title = pick("title")
        desc = pick("des")
        url = pick("url")
        if not (title or desc or url):
            # 不是卡片 XML（比如纯链接），原样放行让模型自己看。
            return raw.strip()

        body = ""
        if url and bool(getattr(self.cfg, "read_links", False)):
            try:
                from ..core.link_reader import read_share

                max_chars = int(getattr(self.cfg, "link_max_chars", 1200) or 1200)
                body = await self._loop.run_in_executor(
                    None, lambda: read_share(url, max_chars)
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"分享正文读取失败: {exc}")
                body = ""

        parts = [f"[分享] {title or '未命名分享'}"]
        if desc:
            parts.append(desc[:200])
        if body:
            parts.append(f"正文：{body}")
        else:
            logger.debug(f"分享只拿到标题和摘要，正文未取到: {url[:60]}")
        return "\n".join(parts)[:1600]

    async def _emit(self, username: str, row: dict) -> None:
        sender_wxid = str(row.get("sender_username", "") or "")
        if self._resolver is not None:
            try:
                resolved = self._resolver.resolve(username, row.get("sender_id"))
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"发送者解析失败: {exc}")
                resolved = ""
            if resolved:
                sender_wxid = resolved
        if sender_wxid.isdigit():
            sender_wxid = ""
        if sender_wxid and sender_wxid == self._self_wxid:
            return
        if int(row.get("sender_id", 0) or 0) in SELF_SENDER_IDS and not sender_wxid:
            return
        content = str(row.get("content", "") or "")
        content = self._strip_prefix(content, sender_wxid)
        quoted_from, quoted_text = self._parse_quote(content)
        if quoted_from:
            content = self._strip_quote_xml(content)
        is_poke = "<patinfo>" in content and self._self_wxid in content
        if is_poke:
            import re as _re
            match = _re.search(r"<pattedusername>(.*?)</pattedusername>", content)
            if match and match.group(1) != self._self_wxid:
                is_poke = False
            content = "[拍一拍]"
        if row.get("type") in ("图片", "image"):
            text = await self._describe_image(username, int(row.get("local_id", 0) or 0))
            content = f"[图片] {text}" if text else "[图片]"
        if row.get("type") in ("语音", "voice"):
            text = await self._transcribe(username, int(row.get("local_id", 0) or 0))
            content = f"[语音] {text}" if text else "[语音]（没听清）"
        row_type = str(row.get("type") or "")
        if row_type in ("文件/链接/卡片", "链接", "appmsg"):
            content = await self._describe_card(content)
        elif row_type in ("动画表情", "表情"):
            room_name = self._names.get(username, "")
            if not room_name:
                try:
                    room_name = await self._loop.run_in_executor(
                        None, lambda: self._db.get_nickname(username)
                    )
                except Exception:  # noqa: BLE001
                    room_name = ""
                if room_name:
                    self._names[username] = room_name
            text = await self._describe_sticker(username, room_name)
            content = f"[动画表情] {text}" if text else "[动画表情]"
        if not content:
            return
        if not quoted_from and not is_poke and row_type not in (
            "文本",
            "text",
            "语音",
            "voice",
            "图片",
            "image",
            "系统消息",
            "文件/链接/卡片",
            "链接",
            "appmsg",
            "动画表情",
            "表情",
        ):
            return
        name = self._names.get(username)
        if not name:
            try:
                name = await self._loop.run_in_executor(
                    None, lambda: self._db.get_nickname(username)
                )
            except Exception:  # noqa: BLE001
                name = None
            name = name or username
            self._names[username] = name
        sender = sender_wxid or ""
        if sender:
            try:
                sender = await self._loop.run_in_executor(
                    None, lambda: self._db.get_nickname(sender_wxid)
                ) or sender_wxid
            except Exception:  # noqa: BLE001
                sender = sender_wxid
        payload = {
            "room_id": username,
            "room_name": name,
            "sender": sender or "群友",
            "content": content,
            "is_group": username.endswith("@chatroom"),
            "is_at": bool(self._self_name) and ("@" + self._self_name) in content,
            "message_id": str(row.get("sort_seq") or row.get("local_id") or ""),
            "is_poke": is_poke,
            "quoted_from": quoted_from,
            "quoted_text": quoted_text,
        }
        logger.info(f"收到 [{name}] {payload['sender']}: {content[:40]}")
        if self._callback:
            await self._callback(payload)

    async def stop(self) -> None:
        self._listening = False
