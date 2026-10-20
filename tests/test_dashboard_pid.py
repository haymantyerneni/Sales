"""The dashboard's "is Harvey running?" probe must never signal the process."""

import os
import subprocess
import sys

from harvey import dashboard


def test_pid_alive_for_running_and_exited_processes():
    assert dashboard._pid_alive(os.getpid())
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert not dashboard._pid_alive(proc.pid)


def test_stale_pid_file_is_cleared(tmp_path, monkeypatch):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    pid_file = tmp_path / "harvey.pid"
    pid_file.write_text(str(proc.pid))
    monkeypatch.setattr(dashboard, "PID_FILE", pid_file)
    monkeypatch.setattr(dashboard, "_harvey_process", None)
    assert dashboard._check_harvey_pid() is None
    assert not pid_file.exists()


def test_windows_probe_never_calls_os_kill(monkeypatch):
    """On Windows os.kill(pid, 0) sends CTRL_C_EVENT — it must not be used."""
    import ctypes
    import types

    calls = []
    k32 = types.SimpleNamespace(
        OpenProcess=lambda *a: 0,  # process not found
        GetExitCodeProcess=lambda *a: 0,
        CloseHandle=lambda *a: None,
    )
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(kernel32=k32), raising=False)
    monkeypatch.setattr(dashboard.sys, "platform", "win32")
    monkeypatch.setattr(dashboard.os, "kill", lambda *a: calls.append(a))
    assert dashboard._pid_alive(12345) is False
    assert calls == []
