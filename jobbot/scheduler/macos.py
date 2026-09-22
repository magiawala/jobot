from __future__ import annotations

import subprocess
from pathlib import Path

LABEL_HOURLY = "com.devanshu.jobbot.hourly"
LABEL_REPORT = "com.devanshu.jobbot.report"
AGENTS = Path.home() / "Library" / "LaunchAgents"


def plist_path(label: str) -> Path:
    return AGENTS / f"{label}.plist"


def status() -> str:
    parts = []
    for label in (LABEL_HOURLY, LABEL_REPORT):
        p = plist_path(label)
        if not p.exists():
            parts.append(f"{label.split('.')[-1]}: not installed")
            continue
        try:
            out = subprocess.run(["launchctl", "list", label], capture_output=True, text=True, timeout=10)
            loaded = out.returncode == 0
        except Exception:  # noqa: BLE001
            loaded = False
        parts.append(f"{label.split('.')[-1]}: {'loaded' if loaded else 'installed but not loaded'}")
    return "; ".join(parts)
