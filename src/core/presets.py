"""档位预设：把一组互相牵制的参数收成三档。

用户只需选「安静 / 正常 / 活跃」，具体数值由这里决定；
高级设置里的同名字段仍然可以覆盖（保存后按当前值生效）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    name: str
    label: str
    desc: str
    score_threshold: int
    min_messages_between_interjections: int
    interject_limit: int
    reply_window_seconds: int
    reply_window_limit: int
    group_max_tokens: int


PRESETS: dict[str, Preset] = {
    "quiet": Preset(
        name="quiet",
        label="安静",
        desc="很少主动接话，被叫到才开口",
        score_threshold=9,
        min_messages_between_interjections=5,
        interject_limit=1,
        reply_window_seconds=600,
        reply_window_limit=2,
        group_max_tokens=60,
    ),
    "normal": Preset(
        name="normal",
        label="正常",
        desc="偶尔接话，不刷屏",
        score_threshold=7,
        min_messages_between_interjections=3,
        interject_limit=2,
        reply_window_seconds=600,
        reply_window_limit=5,
        group_max_tokens=80,
    ),
    "lively": Preset(
        name="lively",
        label="活跃",
        desc="有话就接，适合熟人小群",
        score_threshold=5,
        min_messages_between_interjections=1,
        interject_limit=4,
        reply_window_seconds=600,
        reply_window_limit=10,
        group_max_tokens=100,
    ),
}


def apply_preset(config, name: str) -> bool:
    preset = PRESETS.get(str(name).strip().lower())
    if preset is None:
        return False
    policy = config.policy
    policy.preset = preset.name
    policy.score_threshold = preset.score_threshold
    policy.min_messages_between_interjections = (
        preset.min_messages_between_interjections
    )
    policy.interject_limit = preset.interject_limit
    policy.reply_window_seconds = preset.reply_window_seconds
    policy.reply_window_limit = preset.reply_window_limit
    policy.group_max_tokens = preset.group_max_tokens
    return True


def describe() -> list[dict]:
    return [
        {
            "name": item.name,
            "label": item.label,
            "desc": item.desc,
            "values": {
                "score_threshold": item.score_threshold,
                "min_messages_between_interjections": item.min_messages_between_interjections,
                "interject_limit": item.interject_limit,
                "reply_window_limit": item.reply_window_limit,
                "group_max_tokens": item.group_max_tokens,
            },
        }
        for item in PRESETS.values()
    ]
