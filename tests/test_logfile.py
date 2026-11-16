"""Unattended runs: rotating log file (7 days), PID file, and a process
check that is safe on Windows (os.kill(pid, 0) sends Ctrl+C there)."""

import logging
import os
import subprocess
import sys
from logging.handlers import TimedRotatingFileHandler

from harvey.logfile import log_to_file, log_to_file_from_env, pid_alive, write_pid_file


def test_log_file_rotates_daily_keeping_7_days(tmp_path, monkeypatch):
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        monkeypatch.setenv("HARVEY_LOG_FILE", str(tmp_path / "harvey.log"))
        monkeypatch.delenv("HARVEY_LOG_DAYS", raising=False)
        path = log_to_file_from_env()
        h = [x for x in root.handlers if isinstance(x, TimedRotatingFileHandler)][-1]
        assert h.when == "MIDNIGHT" and h.backupCount == 7
        assert not [x for x in root.handlers if type(x) is logging.StreamHandler]
        logging.getLogger("harvey").info("hello from the heartbeat")
        h.flush()
        assert "hello from the heartbeat" in path.read_text(encoding="utf-8")
    finally:
        for x in list(root.handlers):
            if x not in before:
                root.removeHandler(x)
                x.close()
        for x in before:
            if x not in root.handlers:
                root.addHandler(x)


def test_no_env_means_console_logging(monkeypatch):
    monkeypatch.delenv("HARVEY_LOG_FILE", raising=False)
    assert log_to_file_from_env() is None


def test_pid_alive_never_signals_and_detects_exit(tmp_path):
    assert pid_alive(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pid_alive(child.pid)
        assert child.poll() is None              # probing did not kill it
    finally:
        child.kill()
        child.wait()
    assert not pid_alive(child.pid)
    assert not pid_alive(0)


def test_pid_file_written(tmp_path):
    pid_file = tmp_path / "harvey.pid"
    write_pid_file(pid_file)
    assert pid_file.read_text() == str(os.getpid())


def test_log_to_file_creates_folder(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        p = log_to_file(tmp_path / "sub" / "x.log", days=3)
        assert p.parent.exists()
    finally:
        for x in list(root.handlers):
            if x not in before:
                root.removeHandler(x)
                x.close()
        for x in before:
            if x not in root.handlers:
                root.addHandler(x)
