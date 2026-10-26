"""Web 控制台：状态、配置（带字段说明）、人格、判定记录、回放。"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from src.core.config import (
    FIELD_HELP,
    LISTEN_MODE_HELP,
    LISTEN_MODE_LABELS,
    SECRET_FIELDS,
    looks_masked,
    mask_secret,
    save_config,
    secret_unchanged,
)

STATIC = Path(__file__).resolve().parent / "static"
ROOT = Path(__file__).resolve().parents[2]


class ConfigPatch(BaseModel):
    values: dict


class PersonaPatch(BaseModel):
    file: str = "personas/base.md"
    text: str


class FamilyMember(BaseModel):
    nickname: str
    relation: str = ""
    notes: str = ""


class PresetPatch(BaseModel):
    name: str


class OwnerPatch(BaseModel):
    enabled: bool = True
    wxid: str = ""
    nickname: str = ""
    address_as: str = "老大"
    ai_name: str = "大肥鱼"
    family: list[FamilyMember] = []


class GroupMemoryPatch(BaseModel):
    room_id: str
    text: str = ""
    region: str = ""


class ReplayRequest(BaseModel):
    room_id: str
    day: str | None = None


class SimulateRequest(BaseModel):
    content: str
    room_id: str = "console@chatroom"
    room_name: str = "测试群"
    sender: str = "老大"
    is_group: bool = True
    is_at: bool = False
    is_poke: bool = False


def mask_key(value: str) -> str:
    """展示用掩码（保留旧函数名，行为见 config.mask_secret）。"""
    return mask_secret(value)


def _secret_at(data: dict, dotted: str) -> str:
    """从点号路径取当前真实密钥值，用于判断回写值是不是掩码。"""
    target = data
    for part in dotted.split("."):
        if not isinstance(target, dict):
            return ""
        target = target.get(part)
    return str(target or "")


def _listen_summary(cfg) -> dict:
    wechat = cfg.wechat
    mode = str(getattr(wechat, "listen_mode", "whitelist") or "whitelist")
    groups = [item for item in (wechat.listen_groups or []) if str(item).strip()]
    targets = [item for item in (wechat.private_targets or []) if str(item).strip()]
    listen_private = bool(wechat.listen_private)
    if mode == "whitelist":
        configured = bool(groups) or (listen_private and bool(targets))
    else:
        configured = True
    return {
        "mode": mode,
        "label": LISTEN_MODE_LABELS.get(mode, mode),
        "help": LISTEN_MODE_HELP.get(mode, ""),
        "groups": groups,
        "group_count": len(groups),
        "private": listen_private,
        "private_targets": targets,
        "private_count": len(targets),
        "configured": configured,
    }


def _safe(fn, default):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return default


def create_app(config, bot, memory) -> FastAPI:
    app = FastAPI(title=f"{config.name} 控制台")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    async def status():
        stats = memory.stats()
        return {
            "name": config.name,
            "model": config.provider.model,
            "api_ready": bool(config.provider.api_key),
            "observation": config.policy.observation,
            "search": config.search.enabled,
            "wechat": bool(getattr(bot.channel, "connected", False)),
            "wechat_state": _safe(lambda: bot.channel.wechat_state(), {}),
            "last_send_error": str(getattr(bot.channel, "last_error", "") or ""),
            "outbox": _safe(lambda: len(memory.all_outbox()), 0),
            "stats": stats,
        "usage": memory.usage_stats(),
        "reasoning_ratio": getattr(bot.provider, "last_reasoning_ratio", 0.0),
        "uia": _safe(lambda: bot.channel._uia_ready(), False),
        "foreground_wechat": _safe(lambda: bot.channel._foreground_is_wechat(), False),
        "require_uia": bool(getattr(config.wechat, "require_uia", True)),
        "proactive": bool(config.proactive.enabled),
        "thinking_off": bool(config.provider.disable_thinking),
            "decisions": memory.recent_decisions(20),
            "listen": _listen_summary(config),
        }

    @app.get("/api/usage")
    async def usage():
        current = getattr(bot.provider, "usage", {})
        return {"session": current, "stored": memory.usage_stats()}

    @app.post("/api/notice")
    async def push_notice(patch: dict):
        text = str(patch.get("text", "")).strip()
        if not text:
            return {"success": False, "message": "内容不能为空"}
        room_id = str(patch.get("room_id", "") or getattr(config.owner, "wxid", ""))
        notice_id = memory.add_notice(room_id, text)
        return {"success": True, "id": notice_id}

    @app.get("/api/outbox")
    async def list_outbox():
        return {"items": memory.all_outbox()}

    @app.post("/api/outbox/retry")
    async def retry_outbox(patch: dict):
        outbox_id = int(patch.get("id", 0) or 0)
        items = [item for item in memory.all_outbox() if item["id"] == outbox_id]
        if not items:
            return {"success": False, "message": "找不到这条"}
        item = items[0]
        ok = await bot.channel.send(item["room_id"], item["text"])
        if ok:
            memory.delete_outbox(outbox_id)
            return {"success": True, "message": "已发出"}
        return {"success": False, "message": "还是发不出去，检查窗口和会话"}

    @app.post("/api/outbox/clear")
    async def clear_outbox():
        for item in memory.all_outbox():
            memory.delete_outbox(item["id"])
        return {"success": True}

    @app.post("/api/outbox/delete")
    async def delete_outbox_item(patch: dict):
        memory.delete_outbox(int(patch.get("id", 0) or 0))
        return {"success": True}

    @app.get("/api/vision")
    async def get_vision():
        cfg = config.vision
        return {
            "enabled": cfg.enabled,
            "provider": cfg.provider,
            "model": cfg.model,
            "base_url": cfg.base_url,
            "has_key": bool(cfg.api_key),
            "masked_key": mask_secret(cfg.api_key),
        }

    @app.post("/api/vision")
    async def set_vision(patch: dict):
        cfg = config.vision
        if "enabled" in patch:
            cfg.enabled = bool(patch["enabled"])
        if patch.get("provider"):
            cfg.provider = str(patch["provider"])
        if patch.get("api_key") and not secret_unchanged(patch["api_key"], cfg.api_key):
            cfg.api_key = str(patch["api_key"]).strip()
        if "model" in patch:
            cfg.model = str(patch.get("model") or "")
        if "base_url" in patch:
            cfg.base_url = str(patch.get("base_url") or "")
        save_config(config)
        bot.reload_runtime()
        return {"success": True, "enabled": cfg.enabled, "provider": cfg.provider}

    @app.post("/api/notify/test")
    async def notify_test():
        cfg = config.notify
        if not (cfg.username and cfg.password and cfg.email):
            return {"success": False, "message": "先把邮箱、发件账号、授权码填齐"}
        import smtplib
        from email.mime.text import MIMEText
        from email.header import Header

        try:
            message = MIMEText("小鱼在线，这是一封测试邮件。", "plain", "utf-8")
            message["Subject"] = Header("小鱼测试邮件", "utf-8")
            message["From"] = cfg.username
            message["To"] = cfg.email
            if cfg.smtp_port == 465:
                server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=15)
            else:
                server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15)
                server.starttls()
            server.login(cfg.username, cfg.password)
            server.sendmail(cfg.username, [cfg.email], message.as_string())
            server.quit()
            return {"success": True}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "message": str(exc)[:200]}

    @app.post("/api/model")
    async def switch_model(patch: dict):
        model = str(patch.get("model", "")).strip()
        if not model:
            return {"success": False, "message": "模型名不能为空"}
        config.provider.model = model
        base_url = str(patch.get("base_url", "")).strip()
        if base_url:
            config.provider.base_url = base_url
        api_key = str(patch.get("api_key", "")).strip()
        if api_key and not secret_unchanged(api_key, config.provider.api_key):
            config.provider.api_key = api_key
        if "reasoning_effort" in patch:
            config.provider.reasoning_effort = str(patch.get("reasoning_effort") or "")
        if "disable_thinking" in patch:
            config.provider.disable_thinking = bool(patch.get("disable_thinking"))
        save_config(config)
        bot.reload_runtime()
        return {"success": True, "model": config.provider.model}

    @app.get("/api/presets")
    async def list_presets():
        from src.core.presets import describe
        return {"presets": describe(), "current": config.policy.preset}

    @app.post("/api/preset")
    async def set_preset(patch: PresetPatch):
        from src.core.presets import apply_preset
        if not apply_preset(config, patch.name):
            return {"success": False, "message": "未知档位"}
        save_config(config)
        if hasattr(bot, "reload_runtime"):
            bot.reload_runtime()
        return {"success": True, "current": config.policy.preset}

    @app.get("/api/diagnose")
    async def diagnose():
        checks = []
        key_ok = bool(config.provider.api_key)
        checks.append({"name": "模型 Key", "ok": key_ok, "detail": "已填写" if key_ok else "没有 key"})
        model_ok = bool(config.provider.model and config.provider.base_url)
        checks.append({"name": "模型配置", "ok": model_ok, "detail": f"{config.provider.model} @ {config.provider.base_url}"})
        channel_ok = bool(getattr(bot.channel, "connected", False))
        checks.append({"name": "微信连接", "ok": channel_ok, "detail": "已连接" if channel_ok else "未连接，检查微信是否登录"})
        stats = memory.stats()
        checks.append({"name": "数据库", "ok": stats["messages"] >= 0, "detail": f"消息 {stats['messages']} 条 / 判定 {stats['decisions']} 条"})
        quiet = getattr(bot.schedule, "is_quiet", None)
        in_quiet = bool(quiet()) if callable(quiet) else False
        checks.append({"name": "当前时段", "ok": True, "detail": "静默时段" if in_quiet else "正常时段"})
        return {"checks": checks}

    @app.post("/api/reset")
    async def reset_policy():
        from src.core.config import BudgetConfig, DrawConfig, PolicyConfig, ScheduleConfig
        config.policy = PolicyConfig()
        config.budget = BudgetConfig()
        config.schedule = ScheduleConfig()
        config.draw = DrawConfig()
        save_config(config)
        if hasattr(bot, "reload_runtime"):
            bot.reload_runtime()
        return {"success": True}

    @app.get("/api/owner")
    async def get_owner():
        cfg = config.owner
        family = [
            item for item in memory.list_contacts() if not item.get("is_owner")
        ]
        return {
            "enabled": cfg.enabled,
            "wxid": cfg.wxid,
            "nickname": cfg.nickname,
            "address_as": cfg.address_as,
            "ai_name": cfg.ai_name,
            "family": family,
        }

    @app.post("/api/owner")
    async def set_owner(patch: OwnerPatch):
        config.owner.enabled = patch.enabled
        config.owner.wxid = patch.wxid.strip()
        config.owner.nickname = patch.nickname.strip()
        config.owner.address_as = patch.address_as.strip() or "老大"
        config.owner.ai_name = patch.ai_name.strip() or "大肥鱼"
        save_config(config)
        for member in patch.family:
            key = member.nickname.strip()
            if not key:
                continue
            memory.set_contact(key, key, member.relation.strip(), member.notes.strip(), False)
        if patch.nickname.strip():
            memory.set_contact(
                patch.wxid.strip() or patch.nickname.strip(),
                patch.nickname.strip(),
                "主人",
                "",
                True,
            )
        return {"success": True}

    @app.post("/api/owner/family/delete")
    async def delete_family(patch: dict):
        memory.delete_contact(str(patch.get("user_id", "")))
        return {"success": True}

    @app.get("/api/live")
    async def live(limit: int = 40):
        return {
            "messages": memory.recent_all(limit),
            "decisions": memory.recent_decisions(10),
        }

    @app.get("/api/verdict-stats")
    async def verdict_stats():
        return memory.verdict_stats_today()

    @app.get("/api/schema")
    async def schema():
        return {
            "fields": [
                {
                    "key": key,
                    "label": label,
                    "help": help_text,
                    "example": example,
                    "secret": key in SECRET_FIELDS,
                }
                for key, (label, help_text, example) in FIELD_HELP.items()
            ],
            "secret_fields": list(SECRET_FIELDS),
        }

    @app.get("/api/config")
    async def get_config():
        data = config.model_dump()
        for section, field in (
            ("provider", "api_key"),
            ("search", "api_key"),
            ("vision", "api_key"),
            ("notify", "password"),
        ):
            if data.get(section):
                data[section][field] = mask_key(data[section].get(field, ""))
        data["_secret_fields"] = list(SECRET_FIELDS)
        return data

    @app.post("/api/config")
    async def set_config(patch: ConfigPatch):
        data = config.model_dump()
        kept: list[str] = []
        for key, value in patch.values.items():
            # 掩码/空值不是真密钥：保留磁盘上的原值，不然就把真 key 冲掉了
            if key in SECRET_FIELDS and secret_unchanged(value, _secret_at(data, key)):
                kept.append(key)
                continue
            parts = key.split(".")
            target = data
            for part in parts[:-1]:
                if not isinstance(target.get(part), dict):
                    target[part] = {}
                target = target[part]
            target[parts[-1]] = value
        from src.core.config import AppConfig

        try:
            updated = AppConfig(**data)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "message": f"配置不合法：{exc}"}
        for field in AppConfig.model_fields:
            setattr(config, field, getattr(updated, field))
        save_config(config)
        if hasattr(bot, "reload_runtime"):
            bot.reload_runtime()
        return {"success": True, "kept_secrets": kept}

    @app.get("/api/persona")
    async def get_persona(path: str = "personas/base.md"):
        file = Path(path)
        return {"path": path, "text": file.read_text(encoding="utf-8") if file.exists() else ""}

    @app.post("/api/persona")
    async def set_persona(patch: PersonaPatch):
        file = Path(patch.file)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(patch.text, encoding="utf-8")
        return {"success": True, "path": str(file)}

    @app.get("/api/group-memory")
    async def list_group_memory():
        return {"items": memory.all_group_memory()}

    @app.get("/api/group-memory/one")
    async def get_group_memory(room_id: str):
        return {"room_id": room_id, **memory.get_group_memory(room_id)}

    @app.post("/api/group-memory")
    async def set_group_memory(patch: GroupMemoryPatch):
        memory.set_group_memory(patch.room_id, patch.text, patch.region)
        return {"success": True, **memory.get_group_memory(patch.room_id)}

    @app.post("/api/group-memory/delete")
    async def delete_group_memory(patch: GroupMemoryPatch):
        memory.delete_group_memory(patch.room_id)
        return {"success": True}

    @app.post("/api/group-memory/export")
    async def export_group_memory():
        import json
        import time as _time

        items = memory.all_group_memory()
        backup_dir = ROOT / "data"
        backup_dir.mkdir(parents=True, exist_ok=True)
        path = backup_dir / f"group_memory_backup_{_time.strftime('%Y%m%d_%H%M%S')}.json"
        path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return {"success": True, "count": len(items), "path": str(path)}

    @app.post("/api/group-memory/import")
    async def import_group_memory(patch: dict):
        items = patch.get("items", [])
        if not isinstance(items, list):
            return {"success": False, "message": "格式不对，要是要一个数组"}
        count = 0
        for item in items:
            room_id = str(item.get("room_id", "")).strip()
            if not room_id:
                continue
            memory.set_group_memory(
                room_id, str(item.get("text", "")), str(item.get("region", ""))
            )
            count += 1
        return {"success": True, "count": count}

    @app.post("/api/replay")
    async def replay(req: ReplayRequest):
        rows = memory.export_room(req.room_id, req.day)
        results = []
        for row in rows:
            results.append(
                {
                    "sender": row["sender"],
                    "content": row["content"],
                    "is_group": bool(row["is_group"]),
                }
            )
        return {"count": len(results), "messages": results[:200]}

    @app.post("/api/simulate")
    async def simulate(req: SimulateRequest):
        await bot.handle({
            "content": req.content,
            "room_id": req.room_id,
            "room_name": req.room_name,
            "sender": req.sender,
            "is_group": req.is_group,
            "is_at": req.is_at,
            "is_poke": req.is_poke,
        })
        return {
            "ok": True,
            "stats": memory.stats(),
            "decisions": memory.recent_decisions(5),
        }

    return app
