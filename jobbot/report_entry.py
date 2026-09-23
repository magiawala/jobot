"""launchd entry point for the daily report (python -m jobbot.report_entry)."""
from __future__ import annotations

import sys

if __name__ == "__main__":
    try:
        from .report import send_daily_report
    except ImportError:
        # Phase 6 not built yet - exit 0 so launchd doesn't throttle the agent.
        print("daily report not implemented yet", file=sys.stderr)
        sys.exit(0)
    send_daily_report()
    sys.exit(0)
