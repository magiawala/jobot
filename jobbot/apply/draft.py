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

# Free-text questions answerable from the resume's own facts. Kept to open-ended "tell us about
# your work" style prompts: these recur constantly across postings (they were the single biggest
# source of needs_human in practice), and the answer is grounded in master.json either way.
#
# Deliberately NOT draftable: anything asking for a fact JobBot doesn't have (salary history,
# visa specifics, notice period, references, security clearance, certifications). Those go to
# learned_answers.yaml for a one-time human answer instead of being invented.
# Whether a free-text question can be answered from the resume's own facts.
#
# This started as a list of exact phrasings and kept missing new ones - every posting words
# these differently ("Why Levelpath?", "What is it about Gamma that made you apply?", "Pitch me
# one bold idea..."). Enumerating them does not converge, so draftability is decided by signal
# instead: an open-ended prompt asking for explanation, opinion or an example.
#
# The guard below is what keeps that safe. Anything needing a FACT we do not hold is excluded
# outright, whatever shape the question takes - inventing those puts a false statement on a real
# application, which is far worse than leaving a question for a human.
NON_DRAFTABLE_HINTS = re.compile(
    r"salary|compensation|clearance|citizenship|visa|sponsor|immigration|notice period|"
    r"reference|criminal|felony|fired|asked to resign|background check|citizen|"
    r"certification number|license number|\bgpa\b|country of (your )?birth|"
    r"date of birth|social security|essential functions|disabilit",
    re.I,
)

# Open-ended prompts: these ask you to explain, describe or give an example.
OPEN_ENDED = re.compile(
    r"^\s*(why|how|what|describe|tell|show|walk|pitch|share|explain|give)\b"
    r"|^\s*please (add|list|provide|share|describe|tell|explain)\b"
    r"|why (do|would|are) you"
    r"|\b(example|examples|instance|bullets?)\b"
    r"|tell (us|me) about"
    r"|in your own words",
    re.I,
)

# Yes/no and single-value questions are not essays even when they start with a question word.
CLOSED_FORM = re.compile(
    r"^\s*(are|is|do|does|did|have|has|can|will|would|may|should|were|was)\b"
    r"|^\s*what (is|are) your [\w ]{0,28}?(name|email|phone|address|city|state|zip|pronouns"
    r"|availability|location|timezone|time zone|start date|current employer|go.to)\b"
    r"|^\s*(how many|how much|what year|what month|which)\b",
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
    """True when this is an open-ended question we can answer from the resume.

    Order matters: the fact-guard runs first, so "What was your undergraduate GPA?" and "What is
    the country of your birth?" are excluded even though they are shaped like open questions.
    """
    text = normalize_text(label or "").strip()
    if not text or len(text) < 12:
        return False
    if NON_DRAFTABLE_HINTS.search(text):
        return False
    if CLOSED_FORM.search(text) and not OPEN_ENDED.search(text[text.find(" "):]):
        return False
    return bool(OPEN_ENDED.search(text))


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
