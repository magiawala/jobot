"""Mandatory validator for every Claude-generated resume (variants, tailored resumes, and
form-answer drafts share this fact boundary). Per the spec: reject any employer, title, date,
school, degree, or certification not in the resolved library; reject any skill not in the
keyword bank; reject numbers/metrics that don't appear in the library; reject country-specific
personal details. This is enforced in code, not only in the prompt - a Claude output that fails
never reaches a rendered resume."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from .library import normalize_company

NUMBER_RE = re.compile(r"\d[\d,]*\+?%?")

# A bullet ending on one of these (after stripping trailing punctuation) almost always means the
# model truncated it mid-clause rather than finishing the thought - caught this exact failure
# mode in practice (2 of 3 role variants had bullets silently cut off like "...as a", "...that").
TRUNCATION_STOPWORDS = {
    "a", "an", "the", "of", "to", "and", "or", "in", "for", "with", "on", "at", "by", "from",
    "as", "that", "is", "are", "was", "were", "its", "into", "onto", "over", "under", "via",
}

FORBIDDEN_PERSONAL_TERMS = [
    "nationality", "visa status", "visa sponsorship required", "date of birth", "\bdob\b",
    "marital status", "\bgender\b", "passport", "religion", "photograph attached",
]


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)


@dataclass
class FactBoundary:
    """Built once from master.json + experience_library.json; reused for every validation call."""
    allowed_company_facts: dict[str, dict[str, Any]]  # normalized company -> {title, start_date, end_date, location}
    allowed_numbers_by_company: dict[str, set[str]]  # normalized company -> set of number tokens seen in real bullets
    allowed_skills: set[str]  # lowercased
    allowed_schools: dict[str, dict[str, Any]]  # normalized school -> {degree}


def build_fact_boundary(master: dict[str, Any], library: dict[str, Any]) -> FactBoundary:
    allowed_company_facts = {}
    for e in master.get("experience", []):
        key = normalize_company(e["company"])
        allowed_company_facts[key] = {
            "title": e.get("title", ""), "start_date": e.get("start_date", ""),
            "end_date": e.get("end_date", ""), "location": e.get("location", ""),
        }

    allowed_numbers_by_company: dict[str, set[str]] = {}
    for company_key, bullets in library.get("bullets", {}).items():
        nums: set[str] = set()
        for b in bullets:
            nums.update(m.group(0) for m in NUMBER_RE.finditer(b["text"]))
        allowed_numbers_by_company[company_key] = nums

    allowed_skills: set[str] = set()
    for items in library.get("skills", {}).values():
        allowed_skills.update(i.lower() for i in items)

    allowed_schools = {}
    for source_edu in library.get("education_by_source", {}).values():
        for ed in source_edu:
            key = re.sub(r"[^a-z]", "", ed["school"].lower())
            allowed_schools.setdefault(key, {"degree": ed.get("degree", "")})

    return FactBoundary(allowed_company_facts, allowed_numbers_by_company, allowed_skills, allowed_schools)


def _school_key(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def validate_resume(candidate: dict[str, Any], boundary: FactBoundary) -> ValidationResult:
    errors: list[str] = []

    for e in candidate.get("experience", []):
        key = normalize_company(e.get("company", ""))
        facts = boundary.allowed_company_facts.get(key)
        if facts is None:
            errors.append(f"employer not in resolved library: {e.get('company')!r}")
            continue
        if e.get("title", "").strip() != facts["title"]:
            errors.append(f"title mismatch for {e['company']!r}: got {e.get('title')!r}, expected {facts['title']!r}")
        if e.get("start_date", "").strip() != facts["start_date"] or e.get("end_date", "").strip() != facts["end_date"]:
            errors.append(f"date mismatch for {e['company']!r}: got {e.get('start_date')}-{e.get('end_date')}, "
                          f"expected {facts['start_date']}-{facts['end_date']}")
        if e.get("location") and facts["location"] and e["location"].strip() != facts["location"]:
            errors.append(f"location mismatch for {e['company']!r}: got {e.get('location')!r}, expected {facts['location']!r}")

        allowed_nums = boundary.allowed_numbers_by_company.get(key, set())
        for bullet in e.get("bullets", []):
            words = bullet.strip().rstrip(".,;:").split()
            if words and words[-1].lower() in TRUNCATION_STOPWORDS:
                errors.append(f"bullet looks truncated mid-sentence for {e['company']!r}: {bullet[-60:]!r}")
            for m in NUMBER_RE.finditer(bullet):
                if m.group(0) not in allowed_nums:
                    errors.append(f"unverified metric {m.group(0)!r} in bullet for {e['company']!r}: {bullet[:80]!r}")

    for ed in candidate.get("education", []):
        key = _school_key(ed.get("school", ""))
        if key not in boundary.allowed_schools:
            errors.append(f"school not in resolved library: {ed.get('school')!r}")

    for cat, items in candidate.get("skills", {}).items():
        for item in items:
            if item.lower() not in boundary.allowed_skills:
                errors.append(f"skill not in keyword bank: {item!r}")

    blob = " ".join([
        str(candidate.get("contact", {})),
        " ".join(candidate.get("additional_info", []) or []),
    ]).lower()
    for term in FORBIDDEN_PERSONAL_TERMS:
        if re.search(term, blob):
            errors.append(f"forbidden personal detail present (US resume format): {term!r}")

    return ValidationResult(ok=len(errors) == 0, errors=errors)
