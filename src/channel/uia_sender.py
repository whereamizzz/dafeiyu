"""纯 UIA 发送：不截图、不算坐标、不按 Delete。

已验证（本机微信 4.x）：
- 窗口类名 mmui::MainWindow
- 输入框控件 mmui::ChatInputField，支持 ValuePattern 写入
- 当前会话名可以直接读出来

发送流程：
1. 确认当前会话 == 目标，不是就用库的 open_chat 切过去并复核
2. ValuePattern.SetValue 写文本（不经过键盘）
3. 找「发送」按钮并 Invoke；找不到就聚焦输入框后回车
"""

from __future__ import annotations

import time

from loguru import logger


def _window():
    import uiautomation as auto

    auto.SetGlobalSearchTimeout(2)
    for cls in ("mmui::MainWindow", "WeChatMainWndForPC"):
        try:
            win = auto.WindowControl(searchDepth=1, ClassName=cls)
            if win.Exists(0.5):
                return win
        except Exception:  # noqa: BLE001
            continue
    return None


def _with_uia(fn, *args, **kwargs):
    import uiautomation as auto

    with auto.UIAutomationInitializerInThread():
        return fn(*args, **kwargs)


def probe() -> dict:
    return _with_uia(_probe)


def _probe() -> dict:
    """只读探测：窗口、输入框、发送按钮是否存在。"""
    win = _window()
    if win is None:
        return {"ok": False, "reason": "没找到微信窗口"}
    result = {"ok": True, "window": win.ClassName, "rect": str(win.BoundingRectangle)}
    try:
        edit = win.EditControl(searchDepth=20)
        result["input"] = bool(edit.Exists(1))
        result["input_class"] = edit.ClassName if result["input"] else ""
        result["current_chat"] = edit.Name if result["input"] else ""
        if result["input"]:
            vp = edit.GetValuePattern()
            result["writable"] = not vp.IsReadOnly
    except Exception as exc:  # noqa: BLE001
        result["input_error"] = str(exc)[:80]
    try:
        button = win.ButtonControl(Name="发送", searchDepth=12)
        result["send_button"] = bool(button.Exists(0.5))
    except Exception:  # noqa: BLE001
        result["send_button"] = False
    return result


def fill_input(text: str) -> bool:
    return _with_uia(_fill_input, text)


def _fill_input(text: str) -> bool:
    """只写输入框，不发送；用于测试。"""
    win = _window()
    if win is None:
        return False
    try:
        edit = win.EditControl(searchDepth=20)
        if not edit.Exists(1):
            return False
        edit.GetValuePattern().SetValue(text)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"UIA 写输入框失败: {exc}")
        return False


def send(target: str, text: str) -> bool:
    return _with_uia(_send, target, text)


def _send(target: str, text: str) -> bool:
    win = _window()
    if win is None:
        return False
    try:
        edit = win.EditControl(searchDepth=20)
        if not edit.Exists(1):
            logger.warning("UIA：找不到输入框")
            return False
        current = str(edit.Name or "")
        if target and current and target not in current:
            logger.warning(f"UIA：当前会话是 {current}，不是 {target}，拒发")
            return False
        edit.GetValuePattern().SetValue(text)
        time.sleep(0.2)
        button = win.ButtonControl(Name="发送", searchDepth=12)
        if button.Exists(0.5):
            button.Invoke()
        else:
            edit.SetFocus()
            edit.SendKeys("{Enter}", waitTime=0.05)
        time.sleep(0.3)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"UIA 发送失败: {exc}")
        return False
