"""Matches a recruiter email to the application it's about.

Getting this wrong is worse than not matching at all: a misattributed rejection would credit the
wrong resume variant and score band, quietly corrupting the very metrics the tracking exists to
produce. So matching requires real evidence - the company name as a whole word, or its domain -
and returns nothing when it isn't sure.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ATS senders relay mail on a company's behalf, so their domain says nothing about which company.
ATS_DOMAINS = {
    "greenhouse.io", "us.greenhouse-mail.io", "greenhouse-mail.io", "ashbyhq.com",
    "hire.lever.co", "lever.co", "myworkday.com", "myworkdayjobs.com", "workday.com",
    "smartrecruiters.com", "icims.com", "jobvite.com", "bamboohr.com",
}

# Words that carry no identifying signal once stripped from a company name.
GENERIC = {"inc", "llc", "ltd", "corp", "co", "company", "group", "labs", "technologies",
           "technology", "the", "ai", "io", "app", "hq"}


@dataclass
class Match:
    application_id: int
    company: str
    how: str          # domain | company-name | subject-title
    confidence: str   # high | low


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _tokens(company: str) -> list[str]:
    parts = re.split(r"[^a-z0-9]+", (company or "").lower())
    return [p for p in parts if p and p not in GENERIC and len(p) > 2]


def domain_of(addr: str) -> str:
    return addr.split("@")[-1].lower().strip() if "@" in addr else ""


def match_email(from_addr: str, subject: str, body: str,
                applications: list[dict[str, Any]]) -> Match | None:
    """Returns the application this message is about, or None.

    `applications` should carry at least id, company and title.
    """
    domain = domain_of(from_addr)
    root = ".".join(domain.split(".")[-2:]) if domain else ""
    text = f"{subject}\n{body[:4000]}"
    low = text.lower()

    candidates: list[tuple[Match, int]] = []

    for app in applications:
        company = app.get("company") or ""
        if not company:
            continue
        slug = _slug(company)
        tokens = _tokens(company)

        # 1. the sender's own domain matches the company, e.g. careers@figma.com.
        #    ATS relay domains are excluded - they identify the ATS, not the employer.
        if root and root not in ATS_DOMAINS and slug and len(slug) > 3:
            if slug in _slug(root):
                candidates.append((Match(app["id"], company, "domain", "high"), 100))
                continue

        # 2. the company name appears as a whole word. Substring matching would let "Ramp"
        #    match "rampart" and "Notion" match "notional".
        if tokens and all(re.search(rf"\b{re.escape(t)}\b", low) for t in tokens):
            score = 70 + min(len(tokens) * 5, 15)
            # the job title appearing too makes it much more certain
            title_tokens = [t for t in _tokens(app.get("title") or "") if len(t) > 3]
            if title_tokens and sum(
                    bool(re.search(rf"\b{re.escape(t)}\b", low)) for t in title_tokens) >= 2:
                score += 20
            candidates.append((Match(app["id"], company, "company-name",
                                     "high" if score >= 85 else "low"), score))

    if not candidates:
        return None

    candidates.sort(key=lambda c: -c[1])
    best, best_score = candidates[0]

    # Two different companies both matching means the evidence isn't specific enough to use.
    distinct = {c.company for c, s in candidates if s >= best_score - 10}
    if len(distinct) > 1:
        return None
    return best if best_score >= 70 else None
