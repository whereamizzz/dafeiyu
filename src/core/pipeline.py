"""消息流水线：记录 → 判定 → 生成 → 发送。"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections import defaultdict

from loguru import logger

from .gate import Gate
from .memes import MemeLibrary
from .modes import parse_switch
from .info import Holidays, Weather
from .quick import EchoDetector, QuickReplies

EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]+"
)
SEARCH_HINTS = ("查一下", "搜一下", "搜搜", "是什么", "多少钱", "几号", "什么时候", "新闻", "最新")


class Bot:
    def __init__(
        self,
        config,
        memory,
        provider,
        search,
        persona,
        policy,
        channel,
        schedule=None,
        drawer=None,
    ) -> None:
        self.config = config
        self.memory = memory
        self.provider = provider
        self.search = search
        self.persona = persona
        self.policy = policy
        self.channel = channel
        self.schedule = schedule
        self.drawer = drawer
        self._counters: dict[str, int] = defaultdict(int)
        self._summary_busy: set[str] = set()
        self._last_activity: dict[str, float] = {}
        self._draw_count = 0
        self._draw_day = ""
        self.quick = QuickReplies(config.quick)
        self.echo = EchoDetector(config.echo)
        self._last_quick: dict[str, float] = {}
        self.gate = Gate(config.policy, config.budget)
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._recent_bot: dict[str, list] = {}
        self._fallback_at: dict[str, tuple[str, float]] = {}
        from pathlib import Path as _Path
        self._root = _Path(__file__).resolve().parents[2]
        self._send_failures = 0
        self.memes = MemeLibrary(config.memes)
        self._msgs_since_reply: dict[str, int] = {}
        self._reply_times: dict[str, list] = {}
        self._interject_times: dict[str, list] = {}
        self._last_echo: dict[str, float] = {}
        self.weather = Weather()
        self._last_info: dict[str, float] = {}

    async def resume_pending(self) -> None:
        """启动时把上次没处理完的消息重新排队；太旧的直接丢。"""
        pending = self.memory.list_pending()
        if not pending:
            return
        now = time.time()
        blocked_rooms = self.memory.outbox_rooms()
        fresh = []
        for message in pending:
            ts = float(message.get("ts", 0) or 0)
            room_id = str(message.get("room_id", "") or "")
            age = now - ts if ts else 0.0
            stale = age > self.PENDING_MAX_AGE_SECONDS
            stale_private = age > 300 and not room_id.endswith("@chatroom")
            blocked = room_id in blocked_rooms
            if stale or stale_private or blocked:
                self.memory.drop_pending(int(message.get("_pending_id", 0) or 0))
                continue
            fresh.append(message)
        if not fresh:
            logger.info(f"跳过过期待办 {len(pending)} 条")
            return
        logger.info(f"恢复待办 {len(fresh)} 条")
        for message in fresh:
            try:
                await self.handle({**message, "_deferred": True})
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"恢复待办失败一条: {exc}")

    def reload_runtime(self) -> None:
        """配置保存后重建与配置绑定的运行时对象。"""
        from .schedule import Schedule

        self.schedule = Schedule(self.config.schedule)
        self.quick = QuickReplies(self.config.quick)
        self.echo = EchoDetector(self.config.echo)
        try:
            from .provider import Provider

            self.provider = Provider(self.config.provider)
            self.policy.provider = self.provider
            if getattr(self.policy, "chain", None) is not None:
                self.policy.chain.llm.provider = self.provider
            logger.info(f"模型已切换: {self.config.provider.model}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"模型热切换失败，需要重启: {exc}")
        logger.info("运行时配置已重载")

    # ---- 入口 ----
    async def handle(self, message: dict) -> None:
        """入口：按群排队，群与群并发；同一条消息只处理一次。"""
        room_id = str(message.get("room_id", ""))
        if not message.get("_deferred"):
            key = str(message.get("message_id") or "")
            if key and self.memory.is_processed(room_id, key):
                return
            if key:
                self.memory.mark_processed(room_id, key)
        if message.get("_deferred"):
            await self._process(message)
            return
        queue = self._queues.get(room_id)
        if queue is None:
            queue = asyncio.Queue(maxsize=50)
            self._queues[room_id] = queue
            self._workers[room_id] = asyncio.create_task(self._worker(room_id))
        try:
            message = {**message, "_pending_id": self.memory.add_pending(room_id, message)}
            queue.put_nowait(message)
        except asyncio.QueueFull:
            self.memory.drop_pending(message.get("_pending_id", 0))
            logger.warning(f"队列已满，丢弃消息: {room_id}")

    async def _worker(self, room_id: str) -> None:
        queue = self._queues[room_id]
        while True:
            message = await queue.get()
            try:
                await self._process(message)
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"处理消息失败 [{room_id}]: {exc}")
            finally:
                self.memory.drop_pending(message.get("_pending_id", 0))
                queue.task_done()

    async def _process(self, message: dict) -> None:
        started = time.time()
        room_id = str(message["room_id"])
        content = str(message.get("content", "")).strip()
        if not content:
            return
        is_group = bool(message.get("is_group", False))
        listen_mode = str(
            getattr(self.config.wechat, "listen_mode", "whitelist") or "whitelist"
        ).lower()
        if listen_mode == "off":
            return
        if is_group and bool(getattr(self.config.wechat, "block_groups", False)):
            return
        if is_group and listen_mode != "all":
            # 显式白名单：没配就真的不看，不再默认监听全部群
            allowed_groups = list(self.config.wechat.listen_groups or [])
            if not allowed_groups:
                return
            allowed = any(
                item in (room_id, message.get("room_name", ""))
                or room_id.endswith(item)
                for item in allowed_groups
            )
            if not allowed:
                return
        if not is_group:
            if not self.config.wechat.listen_private:
                return
            if listen_mode != "all":
                targets = list(self.config.wechat.private_targets or [])
                sender = str(message.get("sender", ""))
                room = str(message.get("room_id", ""))
                if not targets:
                    return
                if not any(item in (sender, room) for item in targets):
                    return

        if not message.get("_deferred"):
            self.gate.on_message(room_id)
            self._msgs_since_reply[room_id] = self._msgs_since_reply.get(room_id, 0) + 1

        if message.get("_deferred"):
            pass
        else:
            self.memory.add_message(
            room_id,
            str(message.get("room_name", room_id)),
            str(message.get("sender", "")),
            content,
            is_group,
        )
        self._last_activity[room_id] = time.time()
        self.persona.adjust_mood(room_id, content, self.config.mood)
        self._counters[room_id] += 1

        self.memory.bump_user(
            room_id,
            str(message.get("sender", "")),
            str(message.get("sender", "")),
            content,
        )
        self.echo.feed(str(message.get("sender", "")), content, room_id)

        if content.startswith("[语音]"):
            content = await self._fix_voice_text(room_id, content)

        want_mode = parse_switch(content)
        if want_mode and self._is_owner(message):
            from .modes import list_modes, mode_file

            if want_mode in ("默认", "基础", "日常"):
                self.memory.set_mode(room_id, "", 1)
                if not self.config.policy.observation:
                    await self.channel.send(room_id, "切回来了。")
                return
            if mode_file(self.config.persona.mode_dir, want_mode) is None:
                options = "、".join(list_modes(self.config.persona.mode_dir)) or "无"
                await self.channel.send(room_id, f"没有这个模式，现有：{options}")
                return
            self.memory.set_mode(
                room_id, want_mode, self.config.persona.mode_ttl_minutes
            )
            if not self.config.policy.observation:
                await self.channel.send(
                    room_id,
                    f"切到{want_mode}模式，{self.config.persona.mode_ttl_minutes} 分钟后自动切回。",
                )
            return

        if message.get("is_poke") and self.config.poke.enabled:
            await self._handle_poke(message, room_id)
            return

        if await self._handle_memory_command(message, content):
            return

        if any(token in content for token in self.config.persona.aliases):
            message = {**message, "is_at": True}
        if message.get("quoted_from") and message.get("quoted_from") == getattr(self.channel, "self_wxid", ""):
            message = {**message, "is_at": True}

        quick = self._quick_reply(message, content, is_group)
        if quick:
            await self._send_quick(room_id, quick, private=not is_group)
            return

        if is_group and listen_mode != "at_only":
            echo = self._echo_reply(room_id, str(message.get("sender", "")), content)
            if echo:
                await self._send_quick(room_id, echo)
                return

        direct = bool(message.get("is_at")) or (not is_group)

        if is_group and listen_mode == "at_only" and not direct:
            # 「只回被 @ 的」：不@就只记账，绝不主动插话
            if not message.get("_deferred"):
                self.memory.add_decision(
                    room_id,
                    str(message.get("sender", "")),
                    content,
                    0,
                    "只回被 @ 的（没叫就不插话）",
                    False,
                )
            await self._maybe_summarize(room_id)
            return

        minutes = int(getattr(self.config.policy, "context_minutes", 15))
        snapshot = self.memory.recent_messages_since(
            room_id, minutes, self.config.policy.context_window
        )

        if self.schedule is not None and self.schedule.is_quiet():
            allow = bool(getattr(self.schedule, "quiet_allow_at", False))
            if not (allow and direct):
                self.memory.add_decision(
                    room_id,
                    str(message.get("sender", "")),
                    content,
                    0,
                    "静默时段",
                    False,
                )
                await self._maybe_summarize(room_id)
                return

        if direct:
            accepted, score, reason = True, 10, "必应（@/私聊/引用）"
        else:
            bonus = (
                self.schedule.active_bonus
                if self.schedule is not None and self.schedule.is_active()
                else 0
            )
            now = time.time()
            since_last = now - self.policy._last_reply.get(room_id, 0)
            recent_replies = [
                item for item in self._reply_times.get(room_id, [])
                if now - item < 60
            ]
            self._reply_times[room_id] = recent_replies
            accepted, score, reason = await self.policy.decide(
                message,
                is_at=bool(message.get("is_at", False)),
                threshold_bonus=bonus,
                context=snapshot,
                since_last=since_last,
                consecutive=len(recent_replies),
            )
        probability = float(getattr(self.policy, "last_probability", 0) or 0)

        if not message.get("_deferred"):
            self.memory.add_decision(
                room_id,
                str(message.get("sender", "")),
                content,
                score,
                reason,
                accepted,
                probability,
            )

        if self.config.policy.observation:
            logger.info(f"[观察] {probability:.2f} {reason} <- {content[:40]}")
            await self._maybe_summarize(room_id)
            return
        if not accepted:
            logger.debug(f"不接（{probability:.2f}）：{reason} <- {content[:40]}")
            await self._maybe_summarize(room_id)
            return

        ok, gate_reason, gate_wait = self.gate.check(
            room_id, direct=direct, private=not is_group
        )
        if not ok:
            self.memory.add_decision(
                room_id,
                str(message.get("sender", "")),
                content,
                score,
                gate_reason,
                False,
                probability,
            )
            logger.info(f"门禁拦截：{gate_reason}")
            return

        try:
            reply = await self.generate(
                room_id, content, is_group=is_group, context=snapshot
            )
            reply = await self._ensure_quality(room_id, reply, content, is_group)
            if not reply:
                return
            ok = await self._send_reply(room_id, reply, is_group)
            if ok:
                self.gate.on_reply(room_id, kind="direct" if direct else "interject")
                self._msgs_since_reply[room_id] = 0
                self._reply_times.setdefault(room_id, []).append(time.time())
                elapsed = time.time() - started
                logger.info(
                    f"已回复 [{message.get('room_name', room_id)}] 耗时 {elapsed:.1f}s: {reply[:60]}"
                )
                self.policy._last_reply[room_id] = time.time()
                self.memory.add_message(
                    room_id,
                    str(message.get("room_name", room_id)),
                    "（我）",
                    reply,
                    is_group,
                )
                if is_group and self.memes.enabled and not self.config.policy.observation:
                    meme = self.memes.pick(content, self.persona.mood(room_id, self.config.mood))
                    if meme and hasattr(self.channel, "send_image"):
                        if await self.channel.send_image(room_id, meme):
                            if getattr(self.config.memes, "count_in_budget", True):
                                self.gate.on_reply(room_id, kind="extra")
        finally:
            pass
        await self._maybe_summarize(room_id)

    def _quality_issue(self, room_id: str, reply: str) -> str:
        text = (reply or "").strip()
        if len(text) < 2:
            return "空回复"
        banned = ("作为AI", "作为 AI", "作为人工智能", "我是一个AI", "语言模型", "很高兴为你")
        if any(word in text for word in banned):
            return "自称 AI"
        recent = self._recent_bot.get(room_id, [])
        if text in recent:
            return "复读"
        return ""

    async def _ensure_quality(
        self, room_id: str, reply: str, content: str, is_group: bool
    ) -> str:
        issue = self._quality_issue(room_id, reply)
        if not issue:
            self._remember_reply(room_id, reply)
            return reply
        logger.warning(f"回复质检不合格（{issue}），直接兜底: {str(reply)[:40]}")
        fb = self.config.fallback
        text = getattr(fb, "empty_text", "") or getattr(fb, "timeout_text", "")
        previous = self._fallback_at.get(room_id)
        if text and previous and previous[0] == text and time.time() - previous[1] < 600:
            logger.warning("兜底话术 10 分钟内已经说过，本次保持沉默")
            return ""
        if text:
            self._fallback_at[room_id] = (text, time.time())
        if text:
            self._remember_reply(room_id, text)
            return text
        return ""

    def _remember_reply(self, room_id: str, reply: str) -> None:
        recent = self._recent_bot.setdefault(room_id, [])
        recent.append(str(reply).strip())
        del recent[:-5]

    async def _send_reply(self, room_id: str, text: str, is_group: bool) -> bool:
        cfg = self.config.split
        parts = [text]
        if cfg.enabled and is_group and len(text) >= cfg.min_chars:
            import re as _re
            chunks = [item for item in _re.split(r"(?<=[。！？!?])", text) if item.strip()]
            if len(chunks) > 1:
                parts = chunks[: max(1, int(cfg.max_parts))]
                if len(chunks) > len(parts):
                    parts[-1] = "".join(chunks[len(parts) - 1:])
        ok = False
        for index, part in enumerate(parts):
            ok = await self.channel.send(room_id, part.strip())
            if not ok:
                self._send_failures += 1
                if not self.memory.has_outbox(room_id, part.strip()):
                    self.memory.add_outbox(room_id, part.strip())
                reason = getattr(self.channel, "last_error", "") or "原因是没拿到"
                logger.warning(f"发送失败（{reason}），已放入待发队列: {part[:30]}")
                return False
            if index < len(parts) - 1:
                await asyncio.sleep(random.uniform(cfg.gap_min, cfg.gap_max))
        self._send_failures = 0
        return ok

    async def _handle_poke(self, message: dict, room_id: str) -> None:
        if self.schedule is not None and self.schedule.is_quiet():
            return
        import random as _random
        replies = ("拍我干嘛", "手闲了？", "别戳，尾巴不是按钮。")
        text = _random.choice(replies)
        if not self.config.policy.observation:
            await self.channel.send(room_id, text)
            if self.memes.enabled and hasattr(self.channel, "send_image"):
                meme = self.memes.pick("拍一拍", self.persona.mood(room_id, self.config.mood))
                if meme:
                    await self.channel.send_image(room_id, meme)
        logger.info(f"拍一拍回应 [{room_id}]: {text}")

    # ---- 认主 ----
    def _is_owner(self, message: dict) -> bool:
        cfg = self.config.owner
        if not cfg.enabled:
            return True
        sender_id = str(
            message.get("sender_username") or message.get("sender_id") or ""
        )
        sender = str(message.get("sender", "") or "")
        if cfg.wxid and sender_id == cfg.wxid:
            return True
        if cfg.nickname and sender == cfg.nickname:
            return True
        record = self.memory.get_contact(sender_id) if sender_id else {}
        return bool(record.get("is_owner"))

    async def _handle_memory_command(self, message: dict, content: str) -> bool:
        markers = ("存入memory", "存进memory", "存入 memory", "存进 memory", "记住", "记一下")
        head = content.strip()
        if not any(head.startswith(marker) for marker in markers):
            return False
        if self.schedule is not None and self.schedule.is_quiet() and not message.get("is_at"):
            return False
        if not self._is_owner(message):
            logger.info(f"非主人尝试改记忆，已忽略: {message.get('sender')}")
            return True
        body = content
        for marker in markers:
            index = body.find(marker)
            if index >= 0:
                body = body[index + len(marker):].strip(" ：:，,")
                break
        if not body:
            await self.channel.send(str(message.get("room_id")), "记啥？")
            return True
        room_id = str(message.get("room_id", ""))
        if message.get("is_group"):
            record = self.memory.get_group_memory(room_id)
            text = (record.get("text", "") + "\n" + body).strip()
            self.memory.set_group_memory(room_id, text, record.get("region", ""))
        else:
            sender_id = str(message.get("sender_id", "") or message.get("sender", ""))
            record = self.memory.get_contact(sender_id)
            notes = (record.get("notes", "") + "\n" + body).strip()
            self.memory.set_contact(
                sender_id,
                record.get("nickname", message.get("sender", "")),
                record.get("relation", ""),
                notes,
                bool(record.get("is_owner")),
            )
        logger.info(f"记忆已写入（{message.get('sender')}）: {body[:40]}")
        if not self.config.policy.observation:
            await self.channel.send(room_id, "记下了。")
        return True

    def _owner_block(self) -> str:
        cfg = self.config.owner
        lines = []
        if cfg.enabled:
            who = cfg.nickname or cfg.wxid or "主人"
            lines.append(f"你的主人是 {who}，你要称呼他「{cfg.address_as}」。")
            lines.append(f"你自己叫「{cfg.ai_name}」。")
        for item in self.memory.list_contacts():
            if item.get("is_owner"):
                continue
            part = item.get("nickname") or item.get("user_id")
            if item.get("relation"):
                part += f"（{item['relation']}）"
            if item.get("notes"):
                part += f"：{item['notes'][:60]}"
            lines.append(part)
        if not lines:
            return ""
        return "【主人与家人】\n" + "\n".join(lines)

    async def _fix_voice_text(self, room_id: str, content: str) -> str:
        """语音转写可能听错同音词，用上下文纠一次错，只改字不改意。"""
        raw = content[len("[语音]"):].strip()
        if len(raw) < 2:
            return content
        history = self.memory.recent_messages(room_id, 8)
        lines = [f"{item['sender']}：{str(item['content'])[:80]}" for item in history]
        prompt = (
            "下面是一段语音转写，可能有同音错字。结合上下文把明显的错字改对，"
            "保持原意和口语，不要润色、不要回答内容、不要解释。只输出改好的句子。\n\n"
            f"上下文：\n" + ("\n".join(lines) or "（无）") + f"\n\n转写：{raw[:200]}"
        )
        try:
            fixed = await self.provider.chat(
                [{"role": "user", "content": prompt}],
                system="你只做错别字纠正，不回答、不解释、不加戏。",
                temperature=0.0,
                max_tokens=80,
                timeout=8.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"语音纠错失败，用原文: {exc}")
            return content
        text = str(fixed or "").strip().strip("「」\"'")
        if not text or len(text) > len(raw) * 3:
            return content
        logger.info(f"语音纠错：{raw[:30]} -> {text[:30]}")
        return f"[语音] {text}"

    # ---- 本地秒回 / 跟复读 ----
    def _quick_reply(self, message: dict, content: str, is_group: bool) -> str | None:
        if not self.quick.cfg.enabled:
            return None
        # 静默时段不接本地话术，@ 除外
        if self.schedule is not None and self.schedule.is_quiet():
            if not (getattr(self.schedule, "quiet_allow_at", False) and message.get("is_at")):
                return None
        private = not is_group
        triggered = bool(message.get("is_at")) or self.policy.hard_trigger(content, False)
        if private and not self.quick.cfg.private_reply:
            return None
        if not private and not (triggered and self.quick.cfg.group_on_trigger):
            return None
        room_id = str(message.get("room_id", ""))
        now = time.time()
        if now - self._last_quick.get(room_id, 0) < self.quick.cfg.cooldown:
            return None
        reply = self.quick.match(content)
        if reply:
            self._last_quick[room_id] = now
        return reply

    def _echo_reply(self, room_id: str, sender: str, content: str) -> str | None:
        if self.schedule is not None and self.schedule.is_quiet():
            return None
        now = time.time()
        if now - self._last_echo.get(room_id, 0) < self.echo.cfg.cooldown:
            return None
        reply = self.echo.detect(sender, content, room_id)
        if reply:
            self._last_echo[room_id] = now
        return reply

    async def _send_quick(self, room_id: str, text: str, private: bool = False) -> None:
        if self.config.policy.observation:
            logger.info(f"[观察][本地] {text}")
            return
        ok, reason, _wait = self.gate.check(room_id, direct=True, private=private)
        if not ok:
            logger.debug(f"本地回复被门禁拦下：{reason}")
            return
        if await self.channel.send(room_id, text):
            self.gate.on_reply(room_id, kind="direct")
            logger.info(f"本地秒回 [{room_id}]: {text}")

    # ---- 天气 / 节日 ----
    async def _info_block(self, room_id: str) -> str:
        cfg = self.config.info
        if not cfg.enabled:
            return ""
        lines: list[str] = []
        record = self.memory.get_group_memory(room_id)
        region = str(record.get("region", "")).strip()
        now = time.time()
        due = now - self._last_info.get(room_id, 0) >= cfg.interval_days * 86400
        if cfg.weather and region and due:
            text = await self.weather.fetch(region)
            if text:
                lines.append(text)
            self._last_info[room_id] = now
        if cfg.holidays:
            upcoming = Holidays.upcoming(cfg.holiday_lookahead_days)
            today = Holidays.today_is()
            if today:
                lines.append(f"今天是{today}")
            elif upcoming:
                lines.append(upcoming)
        return "；".join(lines)

    # ---- 生成 ----
    async def generate(
        self, room_id: str, content: str, is_group: bool = False, context=None
    ) -> str:
        system = self.persona.system_prompt(room_id, self.config.mood)
        system += (
            "\n\n【这一轮怎么回】只回应最后一条标了【现在这条】的消息。"
            "历史消息是背景，不要回答历史里已经翻篇的问题。"
            "默认一句话，最多两句。"
            "如果消息以 [语音] 开头，那是本地转写，可能有个别错字："
            "按整体意思理解，不要纠正、不要复述错字，也不要评论转写质量。"
        )
        summary = self.memory.get_summary(room_id)
        if context is not None:
            history = [item for item in context if str(item.get("content", "")) != content]
        else:
            minutes = int(getattr(self.config.policy, "context_minutes", 15))
            history = self.memory.recent_messages_since(
                room_id, minutes, self.config.policy.context_window
            )
        parts = []
        owner_text = self._owner_block()
        if owner_text:
            parts.append(owner_text)
        info_text = await self._info_block(room_id)
        if info_text:
            parts.append(f"【现实信息】{info_text}")
        if summary:
            parts.append(f"【此前群聊摘要】{summary}")
        if self.config.profile.enabled:
            top = self.memory.top_users(room_id, self.config.profile.top_n)
            if top:
                lines = []
                for item in top:
                    nickname = item.get("nickname") or item.get("user_id")
                    lines.append(f"{nickname}（{item.get('messages', 0)} 条，最近说：{str(item.get('last_text', ''))[:20]}）")
                parts.append("【活跃群友】" + "；".join(lines))
        notes = self.memory.user_notes(room_id, 8)
        if notes:
            joined = "；".join(
                f"{item['nickname']}: {item['notes'][:60]}" for item in notes
            )
            parts.append(f"【群友印象】{joined}")

        search_block = await self._search_block(content)

        messages = []
        if parts:
            messages.append({"role": "user", "content": "\n".join(parts)})
        for item in history:
            prefix = item["sender"]
            messages.append({"role": "user", "content": f"{prefix}：{item['content']}"})
        tail = content if not search_block else f"{content}\n\n[搜索资料]\n{search_block}"
        messages.append({
            "role": "user",
            "content": f"【现在这条，只回这个】{tail}",
        })

        try:
            limit = self.config.policy.group_max_tokens if is_group else None
            reply = await self.provider.chat(
                messages, system=system, max_tokens=limit
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"生成失败: {exc}")
            return self.config.fallback.timeout_text
        return self.clean(reply)

    async def _search_block(self, content: str) -> str:
        if not self.config.search.enabled:
            return ""
        if not any(hint in content for hint in SEARCH_HINTS):
            return ""
        results = await self.search.search(content[:80])
        if not results:
            return ""
        return self.search.format_results(results)

    def clean(self, text: str) -> str:
        value = EMOJI.sub("", str(text or "")).strip()
        value = re.sub(r"\s+", " ", value)
        limit = max(40, int(getattr(self.config.policy, "max_reply_chars", 120)))
        if len(value) > limit:
            cut = value[:limit]
            for mark in ("。", "！", "？", "；", ".", "!", "?"):
                index = cut.rfind(mark)
                if index >= limit // 2:
                    return cut[: index + 1]
            return cut.rstrip("，,、 ") + "。"
        return value


    # ---- 无聊画画 ----
    def purge_old(self) -> None:
        try:
            self.memory.purge(30, 7)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"清理旧数据失败: {exc}")

    async def watchdog_loop(self) -> None:
        """看门狗：STOP 文件或连续发送失败时，直接停机。"""
        import os

        cfg = self.config.watchdog
        while True:
            await asyncio.sleep(max(5, int(cfg.check_seconds)))
            if not cfg.enabled:
                continue
            stop_file = self._root / "data" / "STOP"
            if stop_file.exists():
                logger.error("发现 data/STOP，立即停机")
                os._exit(1)
            if self._send_failures >= int(cfg.max_send_failures):
                logger.error(
                    f"连续发送失败 {self._send_failures} 次，看门狗停机"
                )
                os._exit(2)

    OUTBOX_MAX_AGE_SECONDS = 900
    PENDING_MAX_AGE_SECONDS = 900

    async def outbox_loop(self) -> None:
        """补发队列：窗口恢复后把没发出去的消息补上（过期就丢，别半夜翻旧账）。"""
        while True:
            await asyncio.sleep(30)
            try:
                due = self.memory.due_outbox()
                now = time.time()
                for item in due:
                    text = str(item.get("text", ""))
                    room_id = str(item.get("room_id", ""))
                    created = float(item.get("created_at", now) or now)
                    if now - created > self.OUTBOX_MAX_AGE_SECONDS:
                        self.memory.bump_outbox(item["id"], 99)
                        logger.info(
                            f"补发已过期丢弃（{int(now - created)} 秒前）: {text[:24]}"
                        )
                        continue
                    is_group = room_id.endswith("@chatroom")
                    ok, reason, _wait = self.gate.check(
                        room_id, direct=True, private=not is_group
                    )
                    if not ok:
                        logger.info(f"补发暂缓：{reason}")
                        continue
                    attempts = int(item.get("attempts", 0)) + 1
                    if await self.channel.send(room_id, text):
                        self.gate.on_reply(room_id, kind="direct")
                        self.memory.bump_outbox(item["id"], 99)
                        logger.info(f"补发成功: {text[:30]}")
                    else:
                        self.memory.bump_outbox(item["id"], attempts)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"补发循环异常: {exc}")

    async def proactive_loop(self) -> None:
        """主动开口：有要说的先说，没有就随口聊一句。"""
        import random

        while True:
            cfg = self.config.proactive
            await asyncio.sleep(max(1, int(cfg.interval_minutes)) * 60)
            if not cfg.enabled:
                continue
            from datetime import datetime as _dt

            now_str = _dt.now().strftime("%H:%M")
            if not (
                str(getattr(cfg, "active_start", "09:00"))
                <= now_str
                < str(getattr(cfg, "active_end", "23:00"))
            ):
                continue
            day = time.strftime("%Y-%m-%d")
            if day != getattr(self, "_proactive_day", ""):
                self._proactive_day = day
                self._proactive_count = 0
            if getattr(self, "_proactive_count", 0) >= int(cfg.daily_limit):
                continue

            targets = list(cfg.targets)
            if not targets:
                owner_wxid = getattr(self.config.owner, "wxid", "")
                owner_name = getattr(self.config.owner, "nickname", "")
                if owner_wxid:
                    targets = [owner_wxid]
                elif owner_name:
                    targets = [owner_name]
                else:
                    targets = list(self._last_activity.keys())
            if not targets:
                continue
            room_id = targets[0] if len(targets) == 1 else random.choice(targets)
            last = self._last_activity.get(room_id, 0)
            if last and (time.time() - last) / 60 < int(cfg.idle_minutes):
                continue
            if random.random() > float(cfg.probability):
                continue

            notice = self.memory.take_notice()
            if notice:
                room_id = notice.get("room_id") or room_id
                text = str(notice.get("text", "")).strip()
            else:
                text = await self._compose_proactive(room_id)
            if not text:
                continue
            text = await self._ensure_quality(room_id, text, "", False)
            if not text:
                continue
            is_group = str(room_id).endswith("@chatroom")
            ok, _, _ = self.gate.check(room_id, direct=True, private=not is_group)
            if not ok:
                continue
            if await self._send_reply(room_id, text, is_group=is_group):
                self.gate.on_reply(room_id)
                self._proactive_count = getattr(self, "_proactive_count", 0) + 1
                logger.info(f"主动开口 [{room_id}]: {text[:40]}")
            elif notice:
                # 没发出去就放回去，别丢
                self.memory.add_notice(room_id, text)
                logger.warning("主动消息发送失败，已放回待发送队列")

    async def _compose_proactive(self, room_id: str) -> str:
        """让模型自己找个话头，或者报告它注意到的事。"""
        system = self.persona.system_prompt(room_id, self.config.mood)
        system += (
            "\n\n【主动模式】现在没人跟你说话，你要主动发一条。"
            "可以聊你最近看到的、想到的，或者问老大之前提过的事；"
            "只有在系统给了你待转达的内容时才说具体任务；没有就不要编造进度或任务。"
            "一句话，像随手发的，不要「在吗」，不要解释你在主动。"
        )
        history = self.memory.recent_messages(room_id, 6)
        lines = [f"{item.get('sender', '')}：{str(item.get('content', ''))[:60]}" for item in history]
        try:
            return await self.provider.chat(
                [{"role": "user", "content": "最近聊天：\n" + ("\n".join(lines) or "（没有）")}],
                system=system,
                temperature=0.8,
                max_tokens=120,
                timeout=20.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"主动发言生成失败: {exc}")
            return ""

    async def draw_loop(self) -> None:
        """低频巡检：安静够久 + 活跃时段 + 概率触发，画一张发出去。"""
        import random

        while True:
            await asyncio.sleep(60)
            if self.drawer is None or not self.drawer.enabled:
                continue
            if self.schedule is not None and not self.schedule.is_active():
                continue
            cfg = self.config.draw
            today = time.strftime("%Y-%m-%d")
            if today != self._draw_day:
                self._draw_day = today
                self._draw_count = 0
            if self._draw_count >= cfg.daily_limit:
                continue
            targets = list(cfg.targets) or list(self._last_activity.keys())
            if not targets:
                continue
            room_id = max(targets, key=lambda key: self._last_activity.get(key, 0))
            idle = (time.time() - self._last_activity.get(room_id, 0)) / 60.0
            if idle < cfg.idle_minutes:
                continue
            if random.random() > cfg.probability:
                continue
            prompt = await self._compose_draw_prompt(room_id)
            path = await self.drawer.draw(prompt)
            if not path:
                continue
            sent = False
            if hasattr(self.channel, "send_image"):
                sent = await self.channel.send_image(room_id, str(path))
            text = self.clean(await self._compose_draw_caption(room_id, prompt))
            if text:
                await self.channel.send(room_id, text)
            if sent:
                self._draw_count += 1
                logger.info(f"画了一张发给 [{room_id}]: {path}")

    async def _compose_draw_prompt(self, room_id: str) -> str:
        mood = self.persona.mood(room_id, self.config.mood)
        try:
            return await self.provider.chat(
                [{"role": "user", "content": "随便想一个适合画成简笔画的画面，10 个字以内，只输出画面描述。"}],
                system=f"你是一个画画的人，现在心情 {mood}/100。",
                temperature=0.9,
                max_tokens=30,
            )
        except Exception:  # noqa: BLE001
            return "一只发呆的鲸鱼"

    async def _compose_draw_caption(self, room_id: str, prompt: str) -> str:
        try:
            return await self.provider.chat(
                [{"role": "user", "content": f"你刚画了一张图：{prompt}。发一句话配图，15 字以内。"}],
                system=self.persona.system_prompt(room_id, self.config.mood),
                temperature=0.9,
                max_tokens=40,
            )
        except Exception:  # noqa: BLE001
            return "画了张图，看看。"

    # ---- 摘要 ----
    async def _maybe_summarize(self, room_id: str) -> None:
        if self.schedule is not None and self.schedule.is_quiet():
            return
        every = 25
        if self._counters[room_id] % every:
            return
        if room_id in self._summary_busy:
            return
        self._summary_busy.add(room_id)
        try:
            history = self.memory.recent_messages(room_id, 20)
            transcript = "\n".join(
                f"{item['sender']}：{item['content'][:100]}" for item in history
            )
            summary = await self.provider.chat(
                [{"role": "user", "content": transcript}],
                system=(
                    "把这段群聊压成不超过 60 字的中文摘要，只留话题、结论和情绪，"
                    "不要逐句复述，不要评论。"
                ),
                temperature=0.2,
                max_tokens=120,
            )
            if summary:
                self.memory.set_summary(room_id, summary[:200])
                logger.info(f"摘要更新 [{room_id}]: {summary[:40]}")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"摘要失败: {exc}")
        finally:
            self._summary_busy.discard(room_id)
