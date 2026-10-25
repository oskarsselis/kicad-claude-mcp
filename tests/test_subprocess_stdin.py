"""Child processes must not inherit the server's stdin.

The MCP server talks to its client over stdio. A child that inherits that stdin
pipe can block on Windows until the client sends its next message - which it
never does while it waits for the tool's reply. update_pcb_from_schematic hung
this way until its timeout (KiCAD's Python started from the stdio server).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from kicad_claude.adapters import kicad_cli, kicad_python


class _Done:
    returncode = 0
    stdout = "RESULT"
    stderr = ""


def _capture(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return _Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_pcbnew_script_gets_devnull_stdin(monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.setattr(kicad_python, "_ensure_python", lambda: Path("python.exe"))
    kicad_python._run_pcbnew_script("print(1)", "a.kicad_pcb")
    assert calls and calls[0].get("stdin") is subprocess.DEVNULL


def test_kicad_cli_gets_devnull_stdin(monkeypatch):
    calls = _capture(monkeypatch)
    monkeypatch.setattr(kicad_cli, "_ensure_cli", lambda: Path("kicad-cli.exe"))
    kicad_cli._run(["version"])
    assert calls and calls[0].get("stdin") is subprocess.DEVNULL
