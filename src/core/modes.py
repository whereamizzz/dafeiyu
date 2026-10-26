"""人格模式：多套人格，按指令临时切换。

用法（主人专属）：
    换成工作模式 / 切到简洁模式 / 切人格 工作

- 模式文件：personas/modes/<mode>.md
- 切换后默认 60 分钟自动切回基础人格
- 按群/会话隔离，不影响别的群
"""

from __future__ import annotations

import re
from pathlib import Path

SWITCH_PATTERNS = (
    re.compile(r"(?:换成|切到|切换成|进入)([\u4e00-\u9fa5A-Za-z0-9]{1,12})模式"),
    re.compile(r"(?:切人格|换人格)[:： ]?([\u4e00-\u9fa5A-Za-z0-9]{1,12})"),
    re.compile(r"(?:用|开)([\u4e00-\u9fa5A-Za-z0-9]{1,12})模式"),
)


def parse_switch(text: str) -> str:
    for pattern in SWITCH_PATTERNS:
        match = pattern.search(str(text or ""))
        if match:
            return match.group(1)
    return ""


def mode_file(mode_dir: str, name: str) -> Path | None:
    if not name:
        return None
    folder = Path(mode_dir)
    for suffix in (".md", ".txt"):
        path = folder / f"{name}{suffix}"
        if path.exists():
            return path
    return None


def list_modes(mode_dir: str) -> list[str]:
    folder = Path(mode_dir)
    if not folder.exists():
        return []
    return sorted(item.stem for item in folder.glob("*.md"))
