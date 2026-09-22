from __future__ import annotations


def schedule_status() -> str:
    from .. import config
    os_name = config.detect_os()
    if os_name == "macos":
        from .macos import status
        return status()
    return f"scheduler for {os_name} not installed yet"
