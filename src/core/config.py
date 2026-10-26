"""配置模型与默认值。

所有可调项都带 label/help/example，Web 控制台用它渲染悬浮说明，
保证「每个设置项都能被非技术用户看懂」。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# 密钥字段：接口返回时一律掩码，写回时必须识别掩码并保留原值。
# 键用「段.字段」形式，与 /api/config 的点号路径一致。
# ---------------------------------------------------------------------------
SECRET_FIELDS: tuple[str, ...] = (
    "provider.api_key",
    "search.api_key",
    "vision.api_key",
    "notify.password",
)

_MASK_PATTERN = re.compile(r".{4}\*{4,}.{4}", re.DOTALL)


def mask_secret(value: str) -> str:
    """展示用掩码：4 位 + **** + 4 位，短值一律 ***。"""
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "***"
    return text[:4] + "****" + text[-4:]


def looks_masked(value: str) -> bool:
    """这个值是不是掩码展示值（不是真密钥）。"""
    text = str(value or "").strip()
    if not text:
        return False
    if not text.strip("*"):
        return len(text) >= 3
    return bool(_MASK_PATTERN.fullmatch(text))


def secret_unchanged(new_value, current_value: str) -> bool:
    """空值、掩码、或恰好等于旧值掩码，都说明用户没打算改密钥。"""
    text = str(new_value if new_value is not None else "").strip()
    if not text:
        return True
    if looks_masked(text):
        return True
    return bool(current_value) and text == mask_secret(current_value)


# 监听范围：值 -> 界面文案（前端与 /api/status 共用这一套说法）
LISTEN_MODES: tuple[str, ...] = ("whitelist", "at_only", "all", "off")
LISTEN_MODE_LABELS: dict[str, str] = {
    "whitelist": "只回白名单",
    "at_only": "只回被 @ 的",
    "all": "全部监听",
    "off": "全部不监听",
}
LISTEN_MODE_HELP: dict[str, str] = {
    "whitelist": "只有白名单里的群和私聊会回，其余一律不看。最省心，也是默认。",
    "at_only": "所有群都看，但只有被 @、点名、引用或拍一拍时才回，绝不主动插话。",
    "all": "所有群和私聊都会看，也会主动插话。注意会被不熟的群拖下水。",
    "off": "完全静默，只记录不回复。调试或临时停用时用。",
}


class ProviderConfig(BaseModel):
    base_url: str = "https://api.deepseek.com/v1/chat/completions"
    reasoning_effort: str = ""
    disable_thinking: bool = True
    reasoning_headroom: int = 200
    warn_reasoning_ratio: float = 0.5
    api_key: str = ""
    model: str = "deepseek-chat"
    temperature: float = 0.85
    max_tokens: int = 160
    timeout: float = 20.0


class SearchConfig(BaseModel):
    enabled: bool = False
    provider: str = "searxng"  # searxng | tavily
    base_url: str = "http://127.0.0.1:8888/search"
    api_key: str = ""
    max_results: int = 3
    cache_ttl: int = 1800
    timeout: float = 8.0


class PersonaConfig(BaseModel):
    base_file: str = "personas/base.md"
    aliases: list[str] = ["大肥鱼", "肥鱼", "鲸鱼娘", "小鲸鱼", "小鱼"]
    group_files: dict[str, str] = Field(default_factory=dict)
    temporary_state: bool = True
    state_prompt: str = "personas/state.md"
    tone: int = 3  # 1 温和 · 2 偏温和 · 3 中间 · 4 偏毒舌 · 5 毒舌
    mode_dir: str = "personas/modes"
    mode_ttl_minutes: int = 60


class MoodConfig(BaseModel):
    enabled: bool = True
    start: int = 50
    decay_per_hour: int = 4
    insult_delta: int = -18
    praise_delta: int = 10
    quiet_below: int = 15
    cheerful_above: int = 85
    weather_sync: bool = False
    city: str = ""


class BudgetConfig(BaseModel):
    per_hour: int = 30
    per_group_concurrent: int = 1
    min_gap_seconds: int = 3


class PolicyConfig(BaseModel):
    preset: str = "normal"
    triggers: list[str] = Field(
        default_factory=lambda: ["大肥鱼", "鲸鱼娘", "小鲸鱼", "deepseek"]
    )
    score_threshold: int = 7
    judge_enabled: bool = True
    judge_max_tokens: int = 8
    context_window: int = 20
    context_minutes: int = 15
    max_reply_chars: int = 120
    max_consecutive_turns: int = 3
    private_cooldown_ms: int = 1500
    min_messages_between_interjections: int = 3
    interject_window_seconds: int = 300
    interject_limit: int = 2
    reply_window_seconds: int = 600
    reply_window_limit: int = 5
    group_max_tokens: int = 80
    observation: bool = False


class FallbackConfig(BaseModel):
    timeout_text: str = "卡了一下，等会儿再说。"
    no_search_text: str = "这个我查不到，等会儿再试。"
    empty_text: str = "嗯。"


class WeChatConfig(BaseModel):
    backend: str = "auto"
    # 监听范围：whitelist 显式白名单（默认）· at_only 只回被 @ 的 · all 全部监听 · off 不监听
    listen_mode: str = "all"
    listen_groups: list[str] = Field(default_factory=list)
    listen_private: bool = True
    private_targets: list[str] = Field(default_factory=list)
    poll_interval: float = 1.0
    send_timeout: float = 10.0
    require_uia: bool = True
    send_backend: str = "uia"  # uia | ocr
    require_consent: bool = False
    read_links: bool = True
    link_max_chars: int = 1200
    read_stickers: bool = True

    @field_validator("listen_mode")
    @classmethod
    def _normalize_listen_mode(cls, value: str) -> str:
        text = str(value or "").strip().lower()
        return text if text in LISTEN_MODES else "whitelist"

    @field_validator("listen_groups", "private_targets", mode="before")
    @classmethod
    def _split_list(cls, value):
        if isinstance(value, str):
            parts = re.split(r"[\n,，、;；]+", value)
            return [item.strip() for item in parts if item.strip()]
        return value


class QuickConfig(BaseModel):
    enabled: bool = True
    private_reply: bool = True
    group_on_trigger: bool = True
    cooldown: int = 20


class EchoConfig(BaseModel):
    enabled: bool = True
    probability: float = 0.35
    cooldown: int = 120
    max_chars: int = 8


class ProfileConfig(BaseModel):
    enabled: bool = True
    top_n: int = 5


class ExternalDecisionConfig(BaseModel):
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    timeout: float = 8.0


class MemesConfig(BaseModel):
    enabled: bool = True
    probability: float = 0.25
    cooldown_seconds: int = 120
    daily_limit: int = 10
    packs: list[str] = []
    count_in_budget: bool = True


class SplitConfig(BaseModel):
    enabled: bool = False
    min_chars: int = 40
    max_parts: int = 3
    gap_min: float = 0.8
    gap_max: float = 2.5


class PokeConfig(BaseModel):
    enabled: bool = True
    reply_back: bool = True


class WatchdogConfig(BaseModel):
    enabled: bool = True
    check_seconds: int = 30
    max_send_failures: int = 5


class SupervisorConfig(BaseModel):
    """外部守护进程：后端卡死或掉线时自动拉起，跟窗口同生共死。"""

    enabled: bool = True
    stale_seconds: int = 120
    check_seconds: int = 15
    max_restarts: int = 3
    restart_window_seconds: int = 900


class ProactiveConfig(BaseModel):
    enabled: bool = True
    active_start: str = "09:00"
    active_end: str = "23:00"
    idle_minutes: int = 90
    interval_minutes: int = 10
    probability: float = 0.5
    daily_limit: int = 2
    targets: list[str] = []


class VisionConfig(BaseModel):
    enabled: bool = False
    provider: str = "zhipu"  # zhipu / qwen / custom
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    prompt: str = ""
    max_chars: int = 80
    max_tokens: int = 120
    timeout: float = 30.0


class NotifyConfig(BaseModel):
    enabled: bool = False
    email: str = ""
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    username: str = ""
    password: str = ""


class ASRConfig(BaseModel):
    enabled: bool = True
    model: str = "base"
    language: str = "zh"
    device: str = "cpu"
    compute_type: str = "int8"
    endpoint: str = "https://hf-mirror.com"
    initial_prompt: str = "群聊语音，常见词：DSH、大肥鱼、微信、语音、识别、后台、提示词、人格。"


class OwnerConfig(BaseModel):
    enabled: bool = True
    wxid: str = ""
    nickname: str = ""
    address_as: str = "老大"
    ai_name: str = "大肥鱼"


class InfoConfig(BaseModel):
    enabled: bool = True
    weather: bool = True
    holidays: bool = True
    interval_days: int = 3
    holiday_lookahead_days: int = 7


class WebConfig(BaseModel):
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8010


class ScheduleConfig(BaseModel):
    enabled: bool = True
    quiet_start: str = "00:00"
    quiet_end: str = "08:00"
    active_start: str = "09:00"
    active_end: str = "23:00"
    active_bonus: int = 2
    quiet_allow_at: bool = False


class DrawConfig(BaseModel):
    enabled: bool = False
    pil: bool = True
    openai: bool = False
    dashscope: bool = False
    idle_minutes: int = 45
    probability: float = 0.15
    daily_limit: int = 2
    targets: list[str] = Field(default_factory=list)
    api_url: str = ""
    api_key: str = ""
    model: str = ""
    style_prompt: str = "简笔画风格，干净的线条，白底"


class AppConfig(BaseModel):
    name: str = "大肥鱼"
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    search: SearchConfig = Field(default_factory=SearchConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    mood: MoodConfig = Field(default_factory=MoodConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    fallback: FallbackConfig = Field(default_factory=FallbackConfig)
    wechat: WeChatConfig = Field(default_factory=WeChatConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    quick: QuickConfig = Field(default_factory=QuickConfig)
    echo: EchoConfig = Field(default_factory=EchoConfig)
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    info: InfoConfig = Field(default_factory=InfoConfig)
    owner: OwnerConfig = Field(default_factory=OwnerConfig)
    asr: ASRConfig = Field(default_factory=ASRConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    memes: MemesConfig = Field(default_factory=MemesConfig)
    split: SplitConfig = Field(default_factory=SplitConfig)
    poke: PokeConfig = Field(default_factory=PokeConfig)
    decision_external: ExternalDecisionConfig = Field(
        default_factory=ExternalDecisionConfig
    )
    draw: DrawConfig = Field(default_factory=DrawConfig)
    db_path: str = "data/bot.db"


# Web 控制台的字段说明：key -> (label, help, example)
FIELD_HELP: dict[str, tuple[str, str, str]] = {
    "provider.base_url": (
        "模型接口地址",
        "OpenAI 兼容的聊天接口完整地址，DeepSeek 官方是 https://api.deepseek.com/v1/chat/completions。",
        "https://api.deepseek.com/v1/chat/completions",
    ),
    "provider.reasoning_headroom": (
        "思考预留",
        "开启思考时，在回复上限之外多给这么多 token 给思考用，避免正文被挤空。",
        "200",
    ),
    "provider.warn_reasoning_ratio": (
        "思考占比告警",
        "思考 token 超过这个比例就在日志里警告，提示换模型或关思考。",
        "0.5",
    ),
    "provider.disable_thinking": (
        "关闭思考",
        "开：请求里带 thinking disabled，回复快、省 token；关：模型自行思考。",
        "true",
    ),
    "provider.reasoning_effort": (
        "推理强度",
        "low/medium/high，仅部分模型支持（如 flash 的 LOW）；留空不传。",
        "",
    ),
    "provider.model": (
        "模型名",
        "接口支持的模型标识。便宜快选 deepseek-chat，要更强推理可换别的。",
        "deepseek-chat",
    ),
    "provider.temperature": (
        "随机性",
        "0 最稳、1 最放飞。群聊接话建议 0.8-0.9，判分时用 0。",
        "0.85",
    ),
    "provider.max_tokens": (
        "单条回复上限",
        "一条消息最多生成多少 token。群聊 80-160 足够，太大既慢又贵。",
        "160",
    ),
    "search.enabled": (
        "联网搜索",
        "打开后，遇到需要事实/时效的问题会先搜索再回答。",
        "false",
    ),
    "search.provider": (
        "搜索后端",
        "searxng 是自建免费聚合搜索；tavily 是付费 API，质量更稳。",
        "searxng",
    ),
    "search.max_results": (
        "搜索条数",
        "每次最多取几条结果塞给模型。3 条通常够用，多了反而烧 token。",
        "3",
    ),
    "persona.mode_dir": (
        "人格模式目录",
        "每个 .md 是一套临时人格；主人说「换成工作模式」就会切过去。",
        "personas/modes",
    ),
    "persona.mode_ttl_minutes": (
        "模式持续时长",
        "切换后多少分钟自动切回基础人格。",
        "60",
    ),
    "persona.tone": (
        "语气强度",
        "1 温和 · 2 偏温和 · 3 中间 · 4 偏毒舌 · 5 毒舌。会写进提示词，模型照着演。",
        "3",
    ),
    "persona.temporary_state": (
        "临时状态",
        "开启后它会因为被夸/被骂而心情变化，回复跟着变；关掉就永远是基础人格。",
        "true",
    ),
    "mood.decay_per_hour": (
        "心情回落速度",
        "每小时向 50 靠拢多少点。太大情绪一闪就没，太小会记仇一整天。",
        "4",
    ),
    "mood.insult_delta": (
        "被骂时心情变化",
        "负数表示下降。默认 -18，被骂几次就明显低落。",
        "-18",
    ),
    "mood.quiet_below": (
        "低落阈值",
        "心情低于这个值就变消极：短句、少接话，甚至装没看见。",
        "15",
    ),
    "policy.preset": (
        "档位",
        "quiet 安静 / normal 正常 / lively 活跃。选档位会一次性改一组参数，高级里仍可覆盖。",
        "normal",
    ),
    "policy.context_minutes": (
        "上下文只看多少分钟",
        "只把最近这些分钟的消息带进对话。太久之前的话题不翻旧账。",
        "15",
    ),
    "policy.max_consecutive_turns": (
        "连续发言上限",
        "60 秒内说到第几条就主动收尾，把话头让出去。",
        "3",
    ),
    "policy.private_cooldown_ms": (
        "私聊冷却（毫秒）",
        "私聊只防连点，默认 1.5 秒；群聊冷却另算。",
        "1500",
    ),
    "policy.max_reply_chars": (
        "回复字数上限",
        "超过就在句末截断。群聊建议 80-120。",
        "120",
    ),
    "policy.score_threshold": (
        "接话门槛",
        "0-10 分，达到这个分数才开口。调高更安静，调低更活跃。",
        "7",
    ),
    "policy.judge_enabled": (
        "插话判断",
        "开启后每条非 @ 消息先让模型判要不要接；关闭则只认触发词。",
        "true",
    ),
    "policy.observation": (
        "观察模式",
        "只记录判定结果、不真的发消息。先看它想怎么回，合适了再放开。",
        "false",
    ),
    "policy.min_messages_between_interjections": (
        "两次插话之间至少几条",
        "自己说完后，至少等群里 3 条别人的消息再主动开口。@ 你不受限制。",
        "3",
    ),
    "policy.interject_limit": (
        "5 分钟内插话上限",
        "主动搭话的次数上限，被 @ 不算。",
        "2",
    ),
    "policy.reply_window_limit": (
        "窗口内回复上限",
        "比如 600 秒内最多回 5 条。比每小时上限更贴地，防连续插话。",
        "5",
    ),
    "policy.reply_window_seconds": (
        "窗口长度（秒）",
        "配合窗口内回复上限使用。",
        "600",
    ),
    "policy.group_max_tokens": (
        "群聊回复长度上限",
        "群聊越短越像真人。80 约等于两句话。",
        "80",
    ),
    "budget.per_hour": (
        "每小时回复上限",
        "不管多热，一小时最多回几条，超过就不理。防 token 爆表的第一道闸。",
        "30",
    ),
    "budget.per_group_concurrent": (
        "单群并发上限",
        "同一个群同时最多处理几条。设为 1 表示排队，不会几条一起回。",
        "1",
    ),
    "budget.min_gap_seconds": (
        "最小间隔",
        "两次发送之间至少隔多少秒，避免连发被微信风控。",
        "8",
    ),
    "fallback.timeout_text": (
        "超时兜底",
        "模型超时/报错时发这句，不要留空，否则群里会觉得它死了。",
        "卡了一下，等会儿再说。",
    ),
    "fallback.no_search_text": (
        "搜不到兜底",
        "搜索无结果时说的话。",
        "这个我查不到，等会儿再试。",
    ),
    "wechat.listen_mode": (
        "监听范围",
        "whitelist 只回白名单里的群和私聊（最省心）；at_only 所有群都看但只回被 @ 的；all 全部监听且会主动插话（发布默认）；off 全部只记不回。",
        "whitelist",
    ),
    "wechat.listen_groups": (
        "群白名单",
        "群 ID 或群名，一行一个。只有这里列出的群会被处理；留空就是任何群都不回（不会再偷偷监听全部）。配合「监听范围」使用。",
        "123456@chatroom",
    ),
    "wechat.listen_private": (
        "监听私聊",
        "总开关。关闭时私聊消息一律不看；开启后也只处理「私聊白名单」里的人（白名单为空则仍然不回）。",
        "true",
    ),
    "wechat.private_targets": (
        "私聊白名单",
        "允许私聊的 wxid 或昵称，一行一个。为主人单独服务就只填主人的 wxid；留空表示不回任何私聊。",
        "wxid_xxxx",
    ),
    "wechat.backend": (
        "微信后端",
        "auto 自动选；wechatauto 用控件树（不靠截图）；wxauto4 备用。",
        "auto",
    ),
    "wechat.read_links": (
        "读分享正文",
        "开启后，公众号/网页分享卡片会去抓一次正文（最多 8 秒），抓不到就用标题加摘要。关闭则只读标题和摘要，不联网。",
        "true",
    ),
    "wechat.link_max_chars": (
        "分享正文长度",
        "分享卡片抓到的正文最多保留多少字。太长会挤占聊天上下文，1200 字一般够看懂一条新闻。",
        "1200",
    ),
    "wechat.read_stickers": (
        "识别表情包",
        "收到表情时截一张图交给视觉模型看情绪。需要微信窗口在桌面上，关掉就只记「[动画表情]」。",
        "开启",
    ),
    "quick.enabled": (
        "本地秒回",
        "寒暄、报时、摇骰子、抛硬币这些用本地规则直接回，不调模型、不花 token。",
        "true",
    ),
    "quick.private_reply": (
        "私聊秒回",
        "私聊里说「在吗」「早」这类，直接用本地话术回，不用等模型。",
        "true",
    ),
    "quick.group_on_trigger": (
        "群里被叫到时秒回",
        "@ 它或喊trigger词时，寒暄类先走本地，省一轮调用。",
        "true",
    ),
    "quick.cooldown": (
        "秒回冷却（秒）",
        "两次本地秒回之间至少隔多久，避免刷屏。",
        "20",
    ),
    "echo.enabled": (
        "跟复读",
        "群里有人复读同一句短话时，本地跟一句「+1」，纯字符串匹配，零成本。",
        "true",
    ),
    "echo.probability": (
        "跟复读概率",
        "检测到复读后跟一句的概率，1 表示每次都跟。",
        "0.35",
    ),
    "echo.cooldown": (
        "跟复读冷却（秒）",
        "两次跟复读之间至少隔多久。",
        "120",
    ),
    "echo.max_chars": (
        "最长跟读字数",
        "超过这个长度的句子不跟，免得像复读机。",
        "8",
    ),
    "memes.enabled": (
        "发表情包",
        "回到适合的场景时，从本地表情库挑一张发出去。",
        "true",
    ),
    "memes.probability": (
        "表情概率",
        "满足条件后发图的概率，0.25 表示约四分之一。",
        "0.25",
    ),
    "memes.count_in_budget": (
        "表情计入预算",
        "开：发一张图也算一次回复，占配额；关：图片不占。",
        "true",
    ),
    "memes.daily_limit": (
        "每天上限",
        "一天最多几张，防刷图。",
        "10",
    ),
    "split.enabled": (
        "分句发送",
        "长回复拆成 2-3 条短消息发，更像真人打字。",
        "true",
    ),
    "split.min_chars": (
        "分句阈值",
        "超过多少字才拆，太短就别拆。",
        "40",
    ),
    "poke.enabled": (
        "拍一拍回应",
        "被拍一拍时回一句，微信里很有存在感。",
        "true",
    ),
    "persona.aliases": (
        "外号表",
        "这些名字都算叫它，用逗号分隔。",
        "大肥鱼,肥鱼,鲸鱼娘,小鲸鱼,小鱼",
    ),
    "watchdog.enabled": (
        "发送失败自停",
        "连续发不出去（比如微信没登录）就干脆停机，避免空转抢鼠标。注意这只是机器人自己停，"
        "「守护进程」那一组是另一个人，负责卡死时把它重新拉起来。",
        "true",
    ),
    "watchdog.max_send_failures": (
        "失败几次停机",
        "连续发送失败达到这个次数就自杀，避免一直抢鼠标。",
        "5",
    ),
    "supervisor.enabled": (
        "守护进程",
        "窗口打开时顺手拉起一个小看门狗：后端卡死、进程没了，它负责重新拉起来。关掉就只剩窗口管进程。改完要关窗重开才生效。",
        "true",
    ),
    "supervisor.stale_seconds": (
        "卡死判定秒数",
        "心跳多久没更新算卡死。太小会把正常发呆误判，太大又要等太久，120 秒比较稳。",
        "120",
    ),
    "supervisor.check_seconds": (
        "巡查间隔",
        "每隔多少秒看一眼心跳。太勤没必要，太懒恢复慢。",
        "15",
    ),
    "supervisor.max_restarts": (
        "重启次数上限",
        "在下面那个时间窗里最多重启几次，防止坏了之后反复重启刷日志。",
        "3",
    ),
    "supervisor.restart_window_seconds": (
        "重启限流窗口",
        "重启次数按这个时间窗计算，默认 15 分钟。",
        "900",
    ),
    "proactive.enabled": (
        "主动找你",
        "静默一段时间后，主动发消息：有要说的先说，没有就随口找你聊一句。",
        "true",
    ),
    "proactive.active_start": (
        "主动从几点开始",
        "只在两个时间点之间主动找你，避免半夜打扰。",
        "09:00",
    ),
    "proactive.active_end": (
        "主动到几点结束",
        "到点后不再主动开口。",
        "23:00",
    ),
    "proactive.idle_minutes": (
        "静默多久",
        "多久没人说话才考虑主动开口。",
        "90",
    ),
    "proactive.daily_limit": (
        "每天上限",
        "主动发消息最多几次，防打扰。",
        "2",
    ),
    "proactive.targets": (
        "发给谁",
        "留空就发给主人私聊；也可以填群名。",
        "",
    ),
    "vision.enabled": (
        "识图",
        "收到图片先让视觉模型描述一句，再当普通消息处理。",
        "false",
    ),
    "vision.provider": (
        "识图服务",
        "zhipu 免费 / qwen 便宜准 / custom 自填。",
        "zhipu",
    ),
    "vision.api_key": (
        "识图 Key",
        "对应厂商的 API Key，和对话模型的 key 不是同一个。",
        "",
    ),
    "notify.email": (
        "邮箱",
        "给小鱼的收件邮箱；填好 SMTP 后可以发测试信。",
        "",
    ),
    "notify.smtp_host": (
        "SMTP 服务器",
        "QQ 邮箱 smtp.qq.com；163 邮箱 smtp.163.com。",
        "smtp.qq.com",
    ),
    "notify.username": (
        "发件账号",
        "一般就是你的邮箱地址。",
        "",
    ),
    "notify.password": (
        "授权码",
        "不是登录密码，是邮箱设置里生成的 SMTP 授权码。",
        "",
    ),
    "asr.enabled": (
        "语音转文字",
        "微信语音条先在本地转成文字再回；不联网、不花钱，首次使用会下载模型。",
        "true",
    ),
    "asr.model": (
        "语音模型",
        "base 够用且快；tiny 更快但更不准；small 更准但慢。",
        "base",
    ),
    "asr.language": (
        "识别语言",
        "zh 中文，en 英文；留空自动检测。",
        "zh",
    ),
    "owner.enabled": (
        "开启认主",
        "开：只有主人能改记忆、教它认识人；关：谁都能。",
        "true",
    ),
    "owner.wxid": (
        "主人的微信 ID",
        "填你的微信号或 wxid，用来认人。填错就认不出来。",
        "wxid_xxxx",
    ),
    "owner.nickname": (
        "主人的昵称",
        "微信里显示的名字，作为 wxid 的补充识别。",
        "你的微信昵称",
    ),
    "owner.address_as": (
        "怎么称呼主人",
        "比如「老大」。它会一直这么叫。",
        "老大",
    ),
    "owner.ai_name": (
        "它自己的名字",
        "显示在界面和自我介绍里。",
        "大肥鱼",
    ),
    "info.enabled": (
        "天气节日",
        "按群记忆里的地区查天气，节日用本地日历；查完即弃，不占存储。",
        "true",
    ),
    "info.weather": (
        "查天气",
        "用 wttr.in 免费接口，不需要 key；群里没填地区就不查。",
        "true",
    ),
    "info.holidays": (
        "查节日",
        "本地日历，未来几天有节日就顺口提一句。",
        "true",
    ),
    "info.interval_days": (
        "天气刷新间隔",
        "隔多少天再查一次天气。查过就丢，不会拿旧数据糊弄人。",
        "3",
    ),
    "info.holiday_lookahead_days": (
        "节日提醒范围",
        "提前几天提节日，比如 7 表示一周内的节日都会提。",
        "7",
    ),
    "profile.enabled": (
        "群友画像",
        "本地统计谁活跃、发了多少条、最近说了啥，回答时带上一小段，不额外调用模型。",
        "true",
    ),
    "profile.top_n": (
        "画像人数",
        "每次最多带几个活跃群友的信息进提示词。",
        "5",
    ),
    "schedule.enabled": (
        "启用作息",
        "开：区分静默/活跃/正常三段；关：一整天都按正常处理。",
        "true",
    ),
    "schedule.quiet_allow_at": (
        "静默期允许 @",
        "开：夜里被 @ 也能叫醒；关：静默期谁叫都不回。",
        "false",
    ),
    "schedule.active_bonus": (
        "活跃加成",
        "活跃时段把接话门槛降低多少分。2 表示 7 分变 5 分，更容易接话。",
        "2",
    ),
    "schedule.quiet_start": (
        "静默开始",
        "这个时间之后机器人不主动说话、不画图、非 @ 消息也不接，省电省 token。",
        "00:00",
    ),
    "schedule.quiet_end": (
        "静默结束",
        "到点恢复正常。跨天也支持，比如 23:00 到 08:00。",
        "08:00",
    ),
    "schedule.active_start": (
        "活跃开始",
        "主动行为（画图、主动发言）只在这个区间里发生。",
        "09:00",
    ),
    "schedule.active_end": (
        "活跃结束",
        "过了这个点就不再主动，除非有人叫它。",
        "23:00",
    ),
    "draw.enabled": (
        "允许画画",
        "开关总闸。关掉就永远不画，也不花钱。",
        "false",
    ),
    "draw.pil": (
        "路线：本地简笔画",
        "免费的 PIL 涂鸦，零成本，风格简单，适合当表情包。",
        "true",
    ),
    "draw.openai": (
        "路线：OpenAI 兼容绘图",
        "填 API 地址和 key 后启用，效果好但按张计费。",
        "false",
    ),
    "draw.dashscope": (
        "路线：通义万相",
        "阿里云百炼的绘图接口，需要百炼 key。",
        "false",
    ),
    "draw.idle_minutes": (
        "无聊阈值",
        "群/私聊安静多少分钟后才考虑画一张。",
        "45",
    ),
    "draw.probability": (
        "触发概率",
        "满足条件后，每秒检查一次，这个概率决定画不画。0.15 表示约一成半。",
        "0.15",
    ),
    "draw.daily_limit": (
        "每天上限",
        "一天最多画几张，防手滑烧钱。",
        "2",
    ),
    "draw.style_prompt": (
        "画风提示词",
        "给绘图模型的前缀描述，比如「简笔画、白底、干净线条」。",
        "简笔画风格，干净的线条，白底",
    ),
}


def _migrate_listen_scope(data: dict) -> dict:
    """老配置里没有 listen_mode：推断成与升级前一致的值，别让机器人突然不干活。"""
    wechat = data.get("wechat")
    if not isinstance(wechat, dict) or "listen_mode" in wechat:
        return data

    def _items(key: str) -> list[str]:
        raw = wechat.get(key)
        if isinstance(raw, str):
            raw = re.split(r"[\n,，、;；]+", raw)
        return [str(item).strip() for item in (raw or []) if str(item).strip()]

    legacy = not _items("listen_groups") and not _items("private_targets")
    wechat["listen_mode"] = "all" if legacy and wechat.get("listen_private", True) else "whitelist"
    return data


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    file = Path(path)
    if not file.exists():
        example = file.with_name("config.example.yaml")
        if example.exists():
            file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    data = yaml.safe_load(file.read_text(encoding="utf-8")) if file.exists() else {}
    return AppConfig(**_migrate_listen_scope(data or {}))


def save_config(config: AppConfig, path: str | Path = "config.yaml") -> None:
    Path(path).write_text(
        yaml.safe_dump(config.model_dump(), allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
