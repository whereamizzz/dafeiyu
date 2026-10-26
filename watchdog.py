"""看门狗：后端事件循环卡死或进程退出时，自动把后端拉起来。

只由 app_window 拉起，跟窗口是一根绳上的：窗口一关，它就被作业对象收走，
父进程没了也会自己退，不会留下来独自找微信。

阈值、次数、端口都从 config.yaml 的 supervisor 节读。
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
HB = DATA / "heartbeat.json"
STOP = DATA / "stop.flag"
LOCK = DATA / "watchdog.lock"
LOG = ROOT / "logs" / "watchdog.log"
HOST = "127.0.0.1"
CREATE_NO_WINDOW = 0x08000000

DEFAULTS = {
    "port": 8010,
    "stale_seconds": 120,
    "check_seconds": 15,
    "max_restarts": 3,
    "restart_window_seconds": 900,
}

PARENT_PID = int(os.environ.get("DFY_PARENT_PID") or 0)


def log(message: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except Exception:  # noqa: BLE001
        pass


def load_settings() -> dict:
    settings = dict(DEFAULTS)
    try:
        import yaml

        config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        port = (config.get("web") or {}).get("port")
        if port:
            settings["port"] = int(port)
        section = config.get("supervisor") or {}
        for key in ("stale_seconds", "check_seconds", "max_restarts", "restart_window_seconds"):
            if section.get(key) is not None:
                settings[key] = int(section[key])
    except Exception:  # noqa: BLE001
        pass
    return settings


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
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


def acquire_lock() -> bool:
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        if LOCK.exists():
            try:
                pid = int(LOCK.read_text(encoding="utf-8").strip() or 0)
            except Exception:  # noqa: BLE001
                pid = 0
            if pid_alive(pid):
                log(f"已经有一个看门狗在跑（PID {pid}），本进程退出")
                return False
        LOCK.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return True


def port_open(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), 1.0):
            return True
    except OSError:
        return False


def read_beat() -> dict:
    try:
        return json.loads(HB.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def kill_backend(pid: int) -> None:
    if pid_alive(pid):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)


def start_backend() -> None:
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    subprocess.Popen(
        [str(python), str(ROOT / "run.py")],
        cwd=str(ROOT),
        creationflags=CREATE_NO_WINDOW,
    )


def restart(reason: str, pid: int, settings: dict) -> None:
    port = settings["port"]
    log(f"{reason}，重启后端（旧 PID {pid}）")
    kill_backend(pid)
    deadline = time.time() + 30
    while time.time() < deadline and port_open(port):
        time.sleep(1)
    if port_open(port):
        log(f"端口 {port} 一直没释放，这次先不重启")
        return
    start_backend()
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(3)
        beat = read_beat()
        if beat and time.time() - float(beat.get("ts") or 0) <= 30:
            log("后端已恢复心跳")
            return
    log("重启后 120 秒内没等到心跳，下一轮再看")


def parent_gone() -> bool:
    pid = PARENT_PID or os.getppid()
    return bool(pid) and not pid_alive(pid)


def main() -> None:
    settings = load_settings()
    if not acquire_lock():
        return
    log(f"看门狗启动（端口 {settings['port']}，阈值 {settings['stale_seconds']} 秒）")
    time.sleep(3)
    started = time.time()
    restarts: list[float] = []
    while True:
        try:
            if STOP.exists():
                log("看到 stop.flag，看门狗退出")
                return
            if parent_gone():
                log("窗口进程已经没了，看门狗退出")
                return
            beat = read_beat()
            now = time.time()
            pid = int(beat.get("pid") or 0)
            age = now - float(beat.get("ts") or 0)
            alive = pid_alive(pid)
            if beat and alive and age <= settings["stale_seconds"]:
                time.sleep(settings["check_seconds"])
                continue
            if not beat and (port_open(settings["port"]) or now - started < 90):
                time.sleep(settings["check_seconds"])
                continue
            restarts = [t for t in restarts if now - t < settings["restart_window_seconds"]]
            if len(restarts) >= settings["max_restarts"]:
                log(f"{settings['restart_window_seconds']} 秒内已经重启 {len(restarts)} 次，先等 10 分钟")
                time.sleep(600)
                continue
            if not beat:
                reason = "没有心跳文件"
            elif not alive:
                reason = f"后端进程 {pid} 已经不在"
            else:
                reason = f"心跳停在 {age:.0f} 秒前"
            restart(reason, pid, settings)
            restarts.append(time.time())
            time.sleep(settings["check_seconds"])
        except Exception as error:  # noqa: BLE001
            log(f"看门狗内部出错：{error!r}")
            time.sleep(settings["check_seconds"])


if __name__ == "__main__":
    main()
