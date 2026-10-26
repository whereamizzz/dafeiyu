"""桌面窗口：控制台就是一个本地窗口。

窗口开着，机器人才工作；窗口一关，后端和看门狗一起停，不留任何 python 进程，
也不会再有抢鼠标找微信的事。

启动方式：双击「肥鱼饭堂.bat」，或 python app_window.py。
已经开着的时候再双击，只会弹一句提醒，不会再起第二套。
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ICON = ROOT / "assets" / "dafeiyu.ico"
CREATE_NO_WINDOW = 0x08000000
GEOM_FILE = ROOT / "data" / "window.json"
DATA = ROOT / "data"
STOP_FLAG = DATA / "stop.flag"
MUTEX_NAME = "Local\\DafeiyuConsole"

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

BUSY_TEXT = (
    "大肥鱼已经在锅里游着了，别急着再下一锅。\n\n"
    "现在这个窗口还开着，两个一起跑只会互相抢锅铲——顺带抢你的鼠标。\n\n"
    "真要重开，先把现有窗口关掉，它会把自己收拾干净，再来双击。"
)

NO_BACKEND_TEXT = (
    "锅还没烧热，后端没起来，机器人没有启动。\n\n"
    "看下 logs\\bot.log 里最后几行，通常能看出是微信没登录、端口被占，还是别的毛病。"
)


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JobKeeper:
    """Windows 作业对象：句柄一关，组里的所有进程（含孙子进程）全灭。

    这样即使 venv 的 python.exe 只是个转发壳、真正干活的 python 是它的子进程，
    关窗口时也不会留下孤儿继续抢鼠标。
    """

    def __init__(self) -> None:
        self.handle = None
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                kernel32.CloseHandle(handle)
                return
            self.handle = handle
            self._kernel32 = kernel32
        except Exception:  # noqa: BLE001
            self.handle = None

    def adopt(self, process: subprocess.Popen | None) -> None:
        if not self.handle or process is None:
            return
        try:
            self._kernel32.AssignProcessToJobObject(self.handle, int(process._handle))
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        if not self.handle:
            return
        try:
            self._kernel32.CloseHandle(self.handle)
        except Exception:  # noqa: BLE001
            pass
        self.handle = None


def message_box(text: str, title: str = "肥鱼饭堂", icon: int = 0x40) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, icon)
    except Exception:  # noqa: BLE001
        pass


def acquire_single_instance() -> bool:
    """同名互斥体：还开着就返回 False（进程一死互斥体自动消失，不会卡住）。"""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            return False
        # 句柄故意不放，进程退出时由系统收回
        return True
    except Exception:  # noqa: BLE001
        return True


def read_config() -> dict:
    try:
        import yaml

        data = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def web_port(config: dict) -> int:
    port = (config.get("web") or {}).get("port") or 8010
    try:
        return int(port)
    except (TypeError, ValueError):
        return 8010


def supervisor_enabled(config: dict) -> bool:
    section = config.get("supervisor") or {}
    if "enabled" in section:
        return bool(section.get("enabled"))
    return True  # 老配置没有这一节时，默认还是有人看门


def load_geometry() -> dict:
    """读上次窗口位置，顺手校验没跑到屏幕外（比如拔了显示器）。"""
    try:
        data = json.loads(GEOM_FILE.read_text("utf-8"))
        width = min(max(int(data["width"]), 960), 4000)
        height = min(max(int(data["height"]), 620), 3000)
        x, y = int(data["x"]), int(data["y"])
        user32 = ctypes.windll.user32
        screen_w, screen_h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        if x < -100 or y < -100 or x > screen_w - 120 or y > screen_h - 60:
            return {"width": width, "height": height}
        return {"x": x, "y": y, "width": width, "height": height}
    except Exception:  # noqa: BLE001
        return {}


def save_geometry(window) -> None:
    try:
        GEOM_FILE.parent.mkdir(parents=True, exist_ok=True)
        GEOM_FILE.write_text(
            json.dumps(
                {
                    "x": window.x,
                    "y": window.y,
                    "width": window.width,
                    "height": window.height,
                }
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def port_ready(port: int, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), 1.0):
                return True
        except OSError:
            time.sleep(0.4)
    return False


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), 1.0):
            return True
    except OSError:
        return False


def sweep_strays() -> list[int]:
    """把不属于本窗口的 bot 残留 python 清掉（run.py / watchdog.py）。"""
    killed = []
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return killed
    for proc in psutil.process_iter(["pid", "name", "cmdline", "cwd"]):
        try:
            if proc.pid == os.getpid():
                continue
            name = (proc.info.get("name") or "").lower()
            if "python" not in name:
                continue
            cmd = " ".join(proc.info.get("cmdline") or [])
            if "app_window.py" in cmd:
                continue
            cwd = proc.info.get("cwd") or ""
            ours = (
                "run.py" in cmd
                or "watchdog.py" in cmd
                or (cwd and Path(cwd) == ROOT)
            )
            if not ours:
                continue
            proc.kill()
            killed.append(proc.pid)
        except Exception:  # noqa: BLE001
            continue
    if killed:
        time.sleep(0.6)
    return killed


def backend_python() -> str:
    # 后端必须用 python.exe：pythonw 没有 stdio，微信库会崩
    return str(Path(sys.executable).with_name("python.exe"))


def main() -> None:
    if not acquire_single_instance():
        message_box(BUSY_TEXT)
        raise SystemExit(0)

    import webview

    STOP_FLAG.unlink(missing_ok=True)
    config = read_config()
    port = web_port(config)

    # 上一次异常退出留下的残留，先清干净，免得两套后端抢端口、抢鼠标
    if port_open(port):
        sweep_strays()

    job = JobKeeper()

    backend = subprocess.Popen(
        [backend_python(), str(ROOT / "run.py")],
        cwd=str(ROOT),
        creationflags=CREATE_NO_WINDOW,
    )
    job.adopt(backend)

    watcher = None
    if supervisor_enabled(config):
        watcher = subprocess.Popen(
            [backend_python(), str(ROOT / "watchdog.py")],
            cwd=str(ROOT),
            creationflags=CREATE_NO_WINDOW,
            env={**os.environ, "DFY_PARENT_PID": str(os.getpid())},
        )
        job.adopt(watcher)

    if not port_ready(port):
        try:
            backend.terminate()
        except Exception:  # noqa: BLE001
            pass
        job.close()
        message_box(NO_BACKEND_TEXT, icon=0x10)
        raise SystemExit("后端没起来，看看 logs/bot.log")

    geom = load_geometry()
    window = webview.create_window(
        "肥鱼饭堂",
        f"http://127.0.0.1:{port}/",
        width=geom.get("width", 1180),
        height=geom.get("height", 820),
        x=geom.get("x"),
        y=geom.get("y"),
        min_size=(960, 620),
        text_select=True,
    )
    stop = threading.Event()

    def geometry_saver():
        while not stop.wait(3.0):
            save_geometry(window)

    threading.Thread(target=geometry_saver, daemon=True).start()
    icon = str(ICON) if ICON.exists() else None
    try:
        webview.start(icon=icon)
    except TypeError:
        webview.start()
    stop.set()
    save_geometry(window)

    # 窗口关闭 → 写停机旗 → 关看门狗 → 关后端 → 作业对象清场 → 兜底扫一遍
    shutdown(backend, watcher, job)


def shutdown(backend, watcher, job) -> None:
    try:
        STOP_FLAG.write_text("closed", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    for proc in (watcher, backend):
        if proc is None:
            continue
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    deadline = time.time() + 5
    for proc in (watcher, backend):
        if proc is None:
            continue
        try:
            proc.wait(timeout=max(0.5, deadline - time.time()))
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    job.close()  # 句柄一关，组里剩下的全被系统杀掉
    sweep_strays()
    try:
        STOP_FLAG.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
