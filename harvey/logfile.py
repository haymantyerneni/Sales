"""File logging for unattended runs (Windows Task Scheduler, services).

When HARVEY_LOG_FILE is set, `harvey run` and `harvey dashboard` log there
instead of the console, rotating at midnight and keeping the last
HARVEY_LOG_DAYS days (default 7). `harvey run` also writes data/harvey.pid
so the dashboard knows Harvey is already running and never starts a second
copy.
"""

from __future__ import annotations

import atexit
import logging
import os
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
PID_FILE = Path(__file__).resolve().parent.parent / "data" / "harvey.pid"


def log_to_file_from_env() -> Path | None:
    path = os.environ.get("HARVEY_LOG_FILE", "").strip()
    if not path:
        return None
    days = int(os.environ.get("HARVEY_LOG_DAYS", "7") or 7)
    return log_to_file(Path(path), days)


def log_to_file(path: Path, days: int = 7) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(str(path), when="midnight", backupCount=days,
                                       encoding="utf-8", delay=False)
    handler.setFormatter(logging.Formatter(FORMAT, DATEFMT))
    root = logging.getLogger()
    for old in list(root.handlers):
        if type(old) is logging.StreamHandler:      # the console handler only
            root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return path


def pid_alive(pid: int) -> bool:
    """Is this process running? On Windows os.kill(pid, 0) is NOT a probe:
    signal 0 is CTRL_C_EVENT, so ask the OS for the exit code instead."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def write_pid_file(pid_file: Path = PID_FILE) -> None:
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    def _remove():
        try:
            if pid_file.read_text().strip() == str(os.getpid()):
                pid_file.unlink()
        except OSError:
            pass
    atexit.register(_remove)
