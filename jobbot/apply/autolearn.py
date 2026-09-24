"""Answers the pending question queue automatically, from facts we already hold.

Questions are batched to Claude with the profile and resume as the ONLY permitted source. The
model must return UNKNOWN when a question needs a fact we don't have - inventing an answer here
would put a false statement on a real application, so an unanswered question is always the
correct outcome over a plausible guess.

Everything it proposes is written with a `source` and `confidence`, and only high-confidence
answers are activated. Low-confidence ones stay pending for a human.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .. import claude_cli, config, log
from . import learned
from .answers import classify_label, derive_answer
from .draft import is_draftable

logger = log.get("apply.autolearn")

BATCH_SIZE = 18

# Entries that are an option fragment rather than a question. Greenhouse checkbox groups record
# each choice separately ("Instagram", "Reddit", "Word of mouth", "Monkey MindPong"), which
# would otherwise burn Claude calls on nonsense and pollute the queue.
OPTION_FRAGMENT = re.compile(
    r"^(instagram|reddit|twitter|slack|linkedin|facebook|youtube|tiktok|github|indeed|glassdoor|"
    r"word of mouth|news/?media|other( \(please specify\))?|friend or family.*|"
    r"campus recruiting.*|event ?[-/].*|women in tech|bwip|watbd|.{0,24} jobs page)$",
    re.I,
)

PROMPT = """Answer these job-application questions for one candidate, using ONLY the facts below.

CRITICAL RULES:
- If a question needs a fact that is NOT in the profile or resume, answer exactly "UNKNOWN".
  Never guess, never infer a fact about the person that isn't stated. A wrong answer here goes
  onto a real job application as a false statement.
- Questions about citizenship, country of birth, security clearances, criminal history, GPA,
  salary history, or anything not present below are ALWAYS "UNKNOWN".
- For yes/no questions, answer exactly "Yes" or "No".
- If the question lists options, answer with one option's exact text.
- For open-ended essay questions, answer "UNKNOWN" (those are drafted separately).
- confidence is "high" only when the facts below directly settle it; otherwise "low".

PROFILE:
{profile}

RESUME FACTS:
{resume}

QUESTIONS (JSON array):
{questions}

Return ONLY a JSON array, one object per question, same order:
[{{"i": 0, "answer": "Yes" or "UNKNOWN", "confidence": "high" or "low", "why": "<8 words>"}}]
"""


def _slim_profile(profile: dict[str, Any]) -> dict[str, Any]:
    keep = ("name", "email", "phone", "location", "links", "work_authorization", "preferences",
            "standard_answers")
    return {k: profile.get(k) for k in keep if profile.get(k)}


def _slim_resume() -> dict[str, Any]:
    path = config.RESUMES_DIR / "master.json"
    if not path.exists():
        return {}
    m = json.loads(path.read_text())
    return {
        "summary": m.get("summary"),
        "skills": m.get("skills"),
        "education": m.get("education"),
        "experience": [{k: e.get(k) for k in ("company", "title", "start_date", "end_date")}
                       for e in m.get("experience", [])],
    }


def prune_option_fragments() -> int:
    """Drops queue entries that are an option label, not a question."""
    data = learned.load()
    before = len(data["pending"])
    data["pending"] = [e for e in data["pending"]
                       if not OPTION_FRAGMENT.match((e.get("question") or "").strip())]
    dropped = before - len(data["pending"])
    if dropped:
        learned.save(data)
    return dropped


def resolve_locally() -> int:
    """Clears entries the code can already answer - no Claude call needed."""
    profile = config.profile()
    data = learned.load()
    keep, resolved = [], 0
    for e in data["pending"]:
        q = e.get("question") or ""
        if classify_label(q)[0] or derive_answer(q, profile) or is_draftable(q):
            resolved += 1
            continue
        keep.append(e)
    if resolved:
        data["pending"] = keep
        learned.save(data)
    return resolved


def auto_answer(limit: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Batches the remaining pending questions to Claude. Returns a summary."""
    profile = config.profile()
    slim_profile = json.dumps(_slim_profile(profile), indent=1)
    slim_resume = json.dumps(_slim_resume())

    data = learned.load()
    pending = [e for e in data["pending"] if not (e.get("answer") or "").strip()]
    pending.sort(key=lambda e: -int(e.get("seen_count", 1)))
    if limit:
        pending = pending[:limit]

    stats = {"asked": 0, "answered_high": 0, "answered_low": 0, "unknown": 0, "calls": 0,
             "proposals": []}

    for start in range(0, len(pending), BATCH_SIZE):
        batch = pending[start:start + BATCH_SIZE]
        payload = [{"i": i, "question": e["question"],
                    "options": (e.get("options") or [])[:14]} for i, e in enumerate(batch)]
        prompt = PROMPT.format(profile=slim_profile, resume=slim_resume,
                               questions=json.dumps(payload, indent=1))
        try:
            raw = claude_cli.call(prompt, purpose="autolearn", timeout=180)
            stats["calls"] += 1
        except (claude_cli.ClaudeUnavailable, claude_cli.ClaudeBadOutput) as e:
            logger.warning("autolearn batch failed: %s", e)
            continue

        try:
            m = re.search(r"\[.*\]", raw, re.S)
            answers = json.loads(m.group(0)) if m else []
        except (json.JSONDecodeError, AttributeError):
            logger.warning("autolearn returned unparseable output; skipping batch")
            continue

        for item in answers:
            idx = item.get("i")
            if not isinstance(idx, int) or idx >= len(batch):
                continue
            entry = batch[idx]
            ans = str(item.get("answer") or "").strip()
            conf = str(item.get("confidence") or "low").lower()
            stats["asked"] += 1
            if not ans or ans.upper() == "UNKNOWN":
                stats["unknown"] += 1
                continue
            if conf != "high":
                stats["answered_low"] += 1
                continue
            stats["answered_high"] += 1
            stats["proposals"].append((entry["question"][:70], ans, item.get("why", "")))
            if not dry_run:
                entry["answer"] = ans
                entry["answered_by"] = "autolearn"
                entry["why"] = item.get("why", "")

    if not dry_run:
        learned.save(data)
        learned.promote_answered()
    return stats
