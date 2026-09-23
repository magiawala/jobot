"""Turns every needs_human into a one-time cost instead of a recurring one.

When a question can't be answered from profile.yaml, it's recorded in
config/learned_answers.yaml with the exact label, the ATS, the options offered, and where it was
seen. You answer it once; every future application that asks the same thing (fuzzy-matched, so
wording drift and per-company phrasing still hit) answers automatically.

This is the main lever on throughput: the same ~20 questions recur across hundreds of postings,
so the needs_human rate should fall steeply over the first few days rather than staying flat.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from rapidfuzz import fuzz

from .. import config, log
from .answers import normalize_label

logger = log.get("apply.learned")

LEARNED_PATH = config.CONFIG_DIR / "learned_answers.yaml"
LEARNED_MATCH_THRESHOLD = 88
_write_lock = threading.Lock()


def _empty() -> dict[str, Any]:
    return {"answers": [], "pending": []}


def load() -> dict[str, Any]:
    if not LEARNED_PATH.exists():
        return _empty()
    with open(LEARNED_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("answers", [])
    data.setdefault("pending", [])
    return data


def save(data: dict[str, Any]) -> None:
    with _write_lock:
        LEARNED_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LEARNED_PATH, "w", encoding="utf-8") as f:
            f.write(HEADER)
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, width=100)


HEADER = """# Learned application answers.
#
# `answers`: questions you've answered once. They're reused automatically on every future
#   application whose question text fuzzy-matches (so slightly different wording still hits).
#   Edit these freely - `answer` is what gets typed, or matched against a dropdown's options.
#
# `pending`: questions JobBot hit that it couldn't answer. Fill in the `answer:` field and move
#   the entry into `answers:` (or just set the answer in place - anything with a non-empty
#   answer is used). Each shows the ATS, the options offered, and an example posting.
#
# Run `jobbot learn` for an interactive pass through everything pending.

"""


def find_answer(label: str) -> str | None:
    """Returns a learned answer for this question, or None."""
    data = load()
    norm = normalize_label(label)
    if not norm:
        return None
    best, best_score = None, 0
    for entry in data.get("answers", []):
        ans = (entry.get("answer") or "").strip()
        if not ans:
            continue
        score = max(
            fuzz.ratio(norm, normalize_label(entry.get("question", ""))),
            fuzz.token_set_ratio(norm, normalize_label(entry.get("question", ""))),
        )
        if score > best_score:
            best, best_score = ans, score
    return best if best_score >= LEARNED_MATCH_THRESHOLD else None


def record_pending(label: str, *, ats: str, options: list[str] | None = None,
                   company: str | None = None, url: str | None = None,
                   required: bool = True, kind: str = "text") -> bool:
    """Logs an unanswerable question for one-time human input. Returns True if newly added."""
    data = load()
    norm = normalize_label(label)
    for bucket in ("answers", "pending"):
        for entry in data.get(bucket, []):
            if fuzz.ratio(norm, normalize_label(entry.get("question", ""))) >= LEARNED_MATCH_THRESHOLD:
                entry["seen_count"] = int(entry.get("seen_count", 1)) + 1
                save(data)
                return False

    data["pending"].append({
        "question": label.strip()[:300],
        "answer": "",
        "kind": kind,
        "ats": ats,
        "options": (options or [])[:25],
        "required": required,
        "seen_count": 1,
        "example_company": company,
        "example_url": url,
        "first_seen": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    save(data)
    logger.info("recorded new unanswered question for learning: %r", label[:70])
    return True


def promote_answered() -> int:
    """Moves any pending entry that now has an answer into `answers`."""
    data = load()
    moved = [e for e in data["pending"] if (e.get("answer") or "").strip()]
    if not moved:
        return 0
    data["pending"] = [e for e in data["pending"] if not (e.get("answer") or "").strip()]
    data["answers"].extend(moved)
    save(data)
    return len(moved)


def stats() -> dict[str, int]:
    data = load()
    pending = data.get("pending", [])
    return {
        "answered": len(data.get("answers", [])),
        "pending": len(pending),
        "pending_blocking": sum(1 for e in pending if e.get("required")),
        "recurrence": sum(int(e.get("seen_count", 1)) for e in pending),
    }
