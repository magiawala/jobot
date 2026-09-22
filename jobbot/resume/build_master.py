"""One-time (re-runnable) build of resumes/master.json from the Claude-structured base
resumes, applying the conflict resolutions Devanshu gave on 2026-09-21 (see resumes/conflicts.md):
Master Resume/US/design/Devanshu_Resume.pdf is authoritative for every disputed date/title/
location, and current location is Boston, MA. Privilon Technologies (Aug 2021-Dec 2022) is
real, uncontested work history that only got trimmed from the newest resume for space, not
because it stopped being true - it's added back in from the UK structured source.

This does not call Claude - it's a pure merge of already-structured JSON plus documented
human decisions, so it's safe to re-run if a source resume changes.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

STRUCTURED_DIR_DEFAULT = Path("resumes/extracted/structured")


def _school_key(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def clean_pdf_spacing(text: str) -> str:
    """Fixes PDF-extraction artifacts like a stray space before a hyphen ('E- commerce'
    -> 'E-commerce'). Whitespace cleanup only - never touches wording or numbers."""
    return re.sub(r"(\w)-\s+(\w)", r"\1-\2", text)


def load_structured(structured_dir: Path) -> dict[str, dict[str, Any]]:
    return {
        label: json.loads((structured_dir / f"{label}.json").read_text())
        for label in ("us_design", "us_research", "uk", "dubai")
    }


def build_master(structured: dict[str, dict[str, Any]]) -> dict[str, Any]:
    us_design = structured["us_design"]
    uk = structured["uk"]
    us_research = structured["us_research"]

    privilon = dict(next(e for e in uk["experience"] if "privilon" in e["company"].lower()))
    privilon["title"] = clean_pdf_spacing(privilon["title"])

    education = []
    for base_edu in us_design["education"]:
        merged = dict(base_edu)
        # us_research has the same schools with explicit dates us_design omits; not a conflict,
        # just missing detail in the newest resume - safe to add, changes no fact. Matched on a
        # punctuation-stripped school name since "Indiana University, Indianapolis" vs "Indiana
        # University Indianapolis" otherwise fail an exact-string match.
        base_key = _school_key(base_edu["school"])
        match = next((e for e in us_research["education"] if _school_key(e["school"]) == base_key), None)
        if match and not merged.get("dates") and match.get("dates"):
            merged["dates"] = match["dates"]
        education.append(merged)

    return {
        "_meta": {
            "built_from": "Master Resume/US/design/Devanshu_Resume.pdf (authoritative per Devanshu, 2026-09-21) "
                          "+ Privilon Technologies from the UK source (real, uncontested, trimmed from the newest "
                          "resume for space) + education dates from the US-research source (missing detail, not a conflict).",
            "conflicts_resolved": "resumes/conflicts.md",
        },
        "contact": {
            "name": "Devanshu Magiawala",
            "email": us_design["contact"]["email"],
            "phone": us_design["contact"]["phone"],
            "location": "Boston, MA",  # confirmed current location, 2026-09-21
            "linkedin": us_design["contact"]["linkedin"],
            "portfolio": us_design["contact"]["portfolio"],
        },
        "summary": us_design["summary"],
        "experience": us_design["experience"] + [privilon],
        "education": education,
        "skills": us_design["skills"],
    }


def main() -> None:
    structured = load_structured(STRUCTURED_DIR_DEFAULT)
    master = build_master(structured)
    out = Path("resumes/master.json")
    out.write_text(json.dumps(master, indent=2))
    print(f"wrote {out} ({len(master['experience'])} experience entries)")


if __name__ == "__main__":
    main()
