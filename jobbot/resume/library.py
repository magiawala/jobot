"""Merges the Claude-structured base resumes + every distinct bullet found across every
resume file (including the ~340 company-tailored variants) into experience_library.json,
and detects cross-version conflicts (different dates/titles/locations for the same employer)
for conflicts.md. Pure code - no AI calls here; the structuring itself already happened once
per genuinely-distinct base resume (see resume/parse.py's dedup analysis for why only a few
base resumes needed a Claude call instead of one per file).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from .. import log
from .parse import extract_all, ExtractedResume

logger = log.get("resume.library")

BULLET_DEDUP_THRESHOLD = 90  # merge bullets more similar than this; keep distinct phrasings below it


def normalize_company(name: str) -> str:
    n = name.lower()
    n = re.sub(r"\(.*?\)", "", n)  # drop parentheticals like "(acquired by My Healing Work)"
    n = re.sub(r"[^a-z0-9 ]", "", n)
    n = re.sub(r"\b(inc|llc|ltd|corp|corporation|co)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def extract_bullets_from_text(text: str) -> list[str]:
    out = []
    for line in text.split("\n"):
        line = line.strip()
        stripped = re.sub(r"^[•●\-\*]\s*", "", line)
        if stripped != line and len(stripped) > 20:
            out.append(stripped)
    return out


@dataclass
class ConflictField:
    field: str
    values: dict[str, str]  # source_label -> value


@dataclass
class RoleConflict:
    company: str
    conflicts: list[ConflictField] = field(default_factory=list)


def compare_experience_across_sources(structured: dict[str, dict[str, Any]]) -> list[RoleConflict]:
    """structured: {source_label: structured_json}. Groups experience entries by normalized
    company name and flags any (start_date, end_date, title, location) that differs across
    sources that both mention that company."""
    by_company: dict[str, dict[str, dict[str, Any]]] = {}
    for label, data in structured.items():
        for exp in data.get("experience", []):
            company = exp.get("company") or ""
            key = normalize_company(company)
            if not key:
                continue
            by_company.setdefault(key, {})[label] = exp

    conflicts: list[RoleConflict] = []
    for key, entries in by_company.items():
        if len(entries) < 2:
            continue
        rc = RoleConflict(company=next(iter(entries.values()))["company"])
        for f in ("start_date", "end_date", "title", "location"):
            values = {label: (e.get(f) or "") for label, e in entries.items()}
            distinct = {v for v in values.values() if v}
            if len(distinct) > 1:
                rc.conflicts.append(ConflictField(field=f, values=values))
        if rc.conflicts:
            conflicts.append(rc)
    return conflicts


def render_conflicts_md(conflicts: list[RoleConflict], notes: list[str]) -> str:
    lines = ["# Resume conflicts to resolve", "",
            "Found by comparing every source resume's structured content. Per the spec, these are",
            "never auto-resolved - answer inline (edit this file or reply in chat) before master.json",
            "is finalized.", ""]
    for note in notes:
        lines.append(f"> {note}")
    lines.append("")
    for i, rc in enumerate(conflicts, 1):
        lines.append(f"## {i}. {rc.company}")
        for cf in rc.conflicts:
            lines.append(f"- **{cf.field}**:")
            for label, val in cf.values.items():
                lines.append(f"  - `{label}`: {val or '(not present)'}")
        lines.append("- **Your answer:** _(fill in the correct value, or say which source is right)_")
        lines.append("")
    return "\n".join(lines)


def build_bullet_library(all_extracted: list[ExtractedResume], company_titles: dict[str, list[str]],
                         root: Path) -> dict[str, list[dict[str, Any]]]:
    """For every (normalized company) known from the structured base resumes, collect every
    distinct bullet mentioning work at that company from ALL resume files (base + tailored
    variants), fuzzy-deduped, each tagged with its source file(s)."""
    library: dict[str, list[dict[str, Any]]] = {c: [] for c in company_titles}

    for r in all_extracted:
        if r.error:
            continue
        rel = str(r.path.relative_to(root))
        # Assign each bullet to whichever known company's block of text it fell under, by
        # slicing the resume text between consecutive company-name occurrences.
        segments = _split_by_company(r.text, company_titles)
        for company_key, seg_text in segments.items():
            for b in extract_bullets_from_text(seg_text):
                _add_bullet(library[company_key], b, rel)
    return library


def _split_by_company(text: str, company_titles: dict[str, list[str]]) -> dict[str, str]:
    """Finds each known company name's occurrence in the text and slices the text between
    consecutive matches, so bullets get attributed to the right employer."""
    markers: list[tuple[int, str]] = []
    for company_key, names in company_titles.items():
        for name in names:
            for m in re.finditer(re.escape(name), text, re.I):
                markers.append((m.start(), company_key))
                break  # first occurrence of this name is enough as a section start
    markers.sort()
    segments: dict[str, str] = {}
    for i, (pos, key) in enumerate(markers):
        end = markers[i + 1][0] if i + 1 < len(markers) else len(text)
        segments.setdefault(key, "")
        segments[key] += text[pos:end] + "\n"
    return segments


def _add_bullet(bucket: list[dict[str, Any]], text: str, source: str) -> None:
    for entry in bucket:
        if fuzz.ratio(entry["text"], text) >= BULLET_DEDUP_THRESHOLD:
            if source not in entry["sources"]:
                entry["sources"].append(source)
            return
    bucket.append({"text": text, "sources": [source]})
