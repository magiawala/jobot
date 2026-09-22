"""Drafts an answer for a short free-text "why this company/role" question - the one case the
spec allows a Claude call for during form filling. Everything else unknown goes to needs_human.

The draft is constrained to facts already in master.json and the job description, and is run
through the same text cleanup as resumes so it doesn't read as machine-written.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .. import claude_cli, config, log
from ..resume.textclean import normalize_text

logger = log.get("apply.draft")

# Only these get a Claude draft. Anything else unknown is a human's call.
DRAFTABLE_PATTERNS = re.compile(
    r"why (do you )?(want to |are you )?(join|work|apply|interested)|why this (company|role|team)|"
    r"why (are you interested|us\b)|what (excites|interests) you|tell us why",
    re.I,
)

MAX_DRAFT_CHARS = 900

PROMPT = """Write a short, specific answer to this job-application question, in Devanshu's voice.

RULES:
- 3-4 sentences, under {max_chars} characters. Plain, direct language.
- Use ONLY facts from the resume JSON and job description below. Invent nothing - no new
  employers, metrics, skills, or claims about the company you weren't given.
- No em-dashes. No "I am excited to" / "I am passionate about" opener. No flattery padding.
- Write as a person, not a cover letter template. Don't restate the question.
- Output ONLY the answer text. No preamble, no quotes, no markdown.

QUESTION: {question}

COMPANY: {company}
ROLE: {title}

JOB DESCRIPTION (first 2000 chars):
{description}

RESUME (facts you may draw on):
{resume}
"""


def is_draftable(label: str) -> bool:
    return bool(DRAFTABLE_PATTERNS.search(label or ""))


def draft_answer(label: str, job: dict[str, Any], job_id: int | None = None) -> str | None:
    """Returns a drafted answer, or None if Claude is unavailable/over cap (caller -> needs_human)."""
    master_path = config.RESUMES_DIR / "master.json"
    if not master_path.exists():
        return None
    master = json.loads(master_path.read_text())
    slim = {
        "summary": master.get("summary"),
        "experience": [
            {k: e.get(k) for k in ("company", "title", "start_date", "end_date", "bullets")}
            for e in master.get("experience", [])[:4]
        ],
        "skills": master.get("skills"),
    }

    prompt = PROMPT.format(
        max_chars=MAX_DRAFT_CHARS,
        question=label.strip(),
        company=job.get("company", ""),
        title=job.get("title", ""),
        description=(job.get("description_text") or "")[:2000],
        resume=json.dumps(slim),
    )
    try:
        text = claude_cli.call(prompt, purpose="application_answer", job_id=job_id, timeout=120)
    except (claude_cli.ClaudeUnavailable, claude_cli.ClaudeBadOutput) as e:
        logger.warning("could not draft answer for %r: %s", label[:60], e)
        return None

    answer = normalize_text(text.strip().strip('"'))
    if not answer or len(answer) > MAX_DRAFT_CHARS * 2:
        return None
    return answer[:MAX_DRAFT_CHARS]
