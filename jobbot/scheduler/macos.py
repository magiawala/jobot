"""launchd scheduling for macOS.

Two things here are not optional, both confirmed against how this actually fails in practice:

1. PATH. launchd does NOT inherit your shell environment, so `claude` (installed at
   ~/.local/bin/claude) is simply not found - and because discovery and scoring don't need
   Claude, the pipeline would appear to work while every tailoring/answer-drafting call failed
   silently, every hour, indefinitely. We write an explicit EnvironmentVariables PATH and also
   pass the venv python by absolute path.

2. caffeinate. `caffeinate -s <command>` holds the system awake only for the lifetime of that
   command, so a run that starts at 03:00 finishes instead of being suspended mid-browser -
   without the battery drain of leaving caffeinate running permanently.

Missed runs while the Mac is asleep are fine by design: discovery is based on "unseen", not on
a time window, so the next run catches up.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
from pathlib import Path

from .. import config

LABEL_HOURLY = "com.devanshu.jobbot.hourly"
LABEL_REPORT = "com.devanshu.jobbot.report"
AGENTS = Path.home() / "Library" / "LaunchAgents"


def plist_path(label: str) -> Path:
    return AGENTS / f"{label}.plist"


def _venv_python() -> Path:
    return config.ROOT / ".venv" / "bin" / "python"


def _launchd_path_value() -> str:
    """PATH for the agent: the venv first, then the dirs a Homebrew/user install actually uses.
    ~/.local/bin matters specifically because that's where `claude` lives."""
    parts = [
        str(config.ROOT / ".venv" / "bin"),
        str(Path.home() / ".local" / "bin"),
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    claude = shutil.which("claude")
    if claude:
        claude_dir = str(Path(claude).parent)
        if claude_dir not in parts:
            parts.insert(1, claude_dir)
    seen: list[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return ":".join(seen)


def _build_plist(label: str, args: list[str], *, interval: int | None = None,
                 calendar: dict[str, int] | None = None) -> dict:
    log_dir = config.LOG_DIR
    plist: dict = {
        "Label": label,
        # caffeinate -s keeps the machine awake only while this run is in flight.
        "ProgramArguments": ["/usr/bin/caffeinate", "-s", str(_venv_python()), "-m", *args],
        "WorkingDirectory": str(config.ROOT),
        "EnvironmentVariables": {
            "PATH": _launchd_path_value(),
            "HOME": str(Path.home()),
            "PYTHONUNBUFFERED": "1",
        },
        "StandardOutPath": str(log_dir / f"{label}.out.log"),
        "StandardErrorPath": str(log_dir / f"{label}.err.log"),
        "RunAtLoad": False,
        "ProcessType": "Background",
    }
    if interval is not None:
        plist["StartInterval"] = interval
    if calendar is not None:
        plist["StartCalendarInterval"] = calendar
    return plist


def install(report_hour: int | None = None) -> list[Path]:
    """Writes and loads both agents. Returns the plist paths written."""
    AGENTS.mkdir(parents=True, exist_ok=True)
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)

    if report_hour is None:
        report_time = str(config.search().get("report_time", "20:00"))
        report_hour = int(report_time.split(":")[0])
        report_minute = int(report_time.split(":")[1]) if ":" in report_time else 0
    else:
        report_minute = 0

    written = []
    hourly = _build_plist(LABEL_HOURLY, ["jobbot.run_entry"], interval=3600)
    report = _build_plist(LABEL_REPORT, ["jobbot.report_entry"],
                          calendar={"Hour": report_hour, "Minute": report_minute})

    for label, data in ((LABEL_HOURLY, hourly), (LABEL_REPORT, report)):
        path = plist_path(label)
        _unload(label)
        with open(path, "wb") as f:
            plistlib.dump(data, f)
        _load(path)
        written.append(path)
    return written


def uninstall() -> list[str]:
    removed = []
    for label in (LABEL_HOURLY, LABEL_REPORT):
        _unload(label)
        path = plist_path(label)
        if path.exists():
            path.unlink()
            removed.append(label)
    return removed


def _load(path: Path) -> None:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)],
                   capture_output=True, text=True, timeout=30)


def _unload(label: str) -> None:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                   capture_output=True, text=True, timeout=30)


def status() -> str:
    parts = []
    for label in (LABEL_HOURLY, LABEL_REPORT):
        short = label.split(".")[-1]
        path = plist_path(label)
        if not path.exists():
            parts.append(f"{short}: not installed")
            continue
        out = subprocess.run(["launchctl", "list", label], capture_output=True, text=True, timeout=10)
        parts.append(f"{short}: {'loaded' if out.returncode == 0 else 'installed but not loaded'}")
    return "; ".join(parts)


def sleep_settings_advice() -> str:
    return (
        "Power settings so hourly runs actually fire:\n"
        "  1. Plugged in, never sleep the system (display sleep is fine):\n"
        "       sudo pmset -c sleep 0\n"
        "  2. Let the Mac wake for scheduled work even on battery:\n"
        "       sudo pmset -a powernap 1\n"
        "  3. Optional - wake the machine daily just before the report:\n"
        "       sudo pmset repeat wakeorpoweron MTWRFSU 19:55:00\n"
        "  Check current settings with:  pmset -g custom\n"
        "Each run is already wrapped in `caffeinate -s`, so the Mac stays awake for the duration\n"
        "of a run and is free to sleep again afterwards."
    )
