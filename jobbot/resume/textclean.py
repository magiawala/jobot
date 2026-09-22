"""Strips typographic 'AI tells' from Claude-generated resume text - most notably the em-dash,
which LLMs overuse stylistically and which doesn't appear anywhere in Devanshu's own actual
resumes (checked: zero em-dashes across all 352 source files). Also normalizes smart quotes and
ellipses to the plain-ASCII style his real resumes use, so generated and copied-through text
reads as one consistent voice. Applied to every Claude resume output before validation/saving -
not just a prompt instruction, since prompts don't reliably prevent this."""
from __future__ import annotations

import re
from typing import Any

_REPLACEMENTS = [
    (re.compile(r"\s*—\s+|\s+—\s*"), ", "),  # em dash used as a clause separator (space on
                                                        # at least one side) -> ", "
    (re.compile(r"—"), "-"),          # bare em dash (e.g. in a date range) -> hyphen
    (re.compile(r"–"), "-"),          # en dash -> hyphen
    (re.compile(r"[‘’]"), "'"),  # curly single quotes/apostrophe -> straight
    (re.compile(r"[“”]"), '"'),  # curly double quotes -> straight
    (re.compile(r"…"), "..."),        # ellipsis character -> three periods
]


def normalize_text(s: str) -> str:
    for pattern, repl in _REPLACEMENTS:
        s = pattern.sub(repl, s)
    s = re.sub(r"\s+,", ",", s)   # a dash->comma swap can leave "word ,"
    s = re.sub(r",\s*,", ",", s)  # or a doubled comma
    s = re.sub(r"[ \t]{2,}", " ", s)
    return s.strip()


def normalize_resume_text(obj: Any) -> Any:
    """Recursively applies normalize_text to every string in a resume JSON structure."""
    if isinstance(obj, str):
        return normalize_text(obj)
    if isinstance(obj, list):
        return [normalize_resume_text(x) for x in obj]
    if isinstance(obj, dict):
        return {k: normalize_resume_text(v) for k, v in obj.items()}
    return obj
