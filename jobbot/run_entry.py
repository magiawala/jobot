"""launchd entry point for the hourly run (python -m jobbot.run_entry)."""
from __future__ import annotations

import sys

from .run import main

if __name__ == "__main__":
    sys.exit(main())
