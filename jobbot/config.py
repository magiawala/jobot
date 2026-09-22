from __future__ import annotations

import os
import platform
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
SCREENSHOT_DIR = ROOT / "screenshots"
RESUMES_DIR = ROOT / "resumes"
SOURCE_RESUMES_DIR = ROOT / "Master Resume"
DB_PATH = DATA_DIR / "jobbot.db"
LOCK_PATH = DATA_DIR / "run.lock"

for _d in (DATA_DIR, LOG_DIR, LOG_DIR / "reports" / "monthly", SCREENSHOT_DIR,
           RESUMES_DIR / "variants", RESUMES_DIR / "tailored", RESUMES_DIR / "templates"):
    _d.mkdir(parents=True, exist_ok=True)

load_dotenv(ROOT / ".env")


def detect_os() -> str:
    s = platform.system()
    return {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}.get(s, s.lower())


def _load(name: str) -> dict[str, Any]:
    p = CONFIG_DIR / f"{name}.yaml"
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=None)
def search() -> dict[str, Any]:
    return _load("search")


@lru_cache(maxsize=None)
def profile() -> dict[str, Any]:
    return _load("profile")


def companies() -> dict[str, Any]:
    # not cached: the resolver appends to it during a run
    return _load("companies")


@lru_cache(maxsize=None)
def sources() -> dict[str, Any]:
    return _load("sources")


@lru_cache(maxsize=None)
def keywords() -> dict[str, Any]:
    return _load("keywords")


def save_companies(data: dict[str, Any]) -> None:
    p = CONFIG_DIR / "companies.yaml"
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=120)


def env(key: str, default: str | None = None) -> str | None:
    return os.environ.get(key, default)


def mode() -> str:
    return (search().get("mode") or "REVIEW").upper()


def limits() -> dict[str, int]:
    d = {"max_tailors_per_day": 10, "max_apps_per_run": 5, "max_apps_per_day": 30, "max_claude_calls_per_day": 25}
    d.update(search().get("limits") or {})
    return d


def api_key_warning() -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ("ANTHROPIC_API_KEY is set in this shell. JobBot never uses it and removes it from every "
                "claude subprocess, but unset it to be safe (it could be billed by other tools).")
    return None
