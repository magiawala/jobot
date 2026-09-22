"""Phase 3 step 2: generate the three role variants (product_design, design_engineer,
ux_design). One Claude call each. Facts (company/title/dates/location) are pinned to
master.json and never touched by the model - only summary, bullet selection/order, and
skill selection/order are role-specific. Every result goes through validate_resume() before
it's accepted; on failure it retries once with the errors listed, then falls back to master.json
verbatim (still truthful, just not role-optimized) rather than risk a fabricated resume."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import claude_cli, config, log
from .library import normalize_company
from .textclean import normalize_resume_text
from .validate import FactBoundary, build_fact_boundary, validate_resume

logger = log.get("resume.variants")

VARIANTS_DIR = config.RESUMES_DIR / "variants"

PROMPT_TEMPLATE = """You are tailoring a resume for a specific role track. This is a reordering/rephrasing/\
selection task, not a writing task - you may ONLY use content given to you below, verbatim or lightly \
reworded. Never invent a new employer, title, date, school, degree, number, metric, or skill.

RULES (critical - a validator will reject any output that breaks these, so follow exactly):
1. "contact" and "education" must be copied through EXACTLY unchanged.
2. Each "experience" entry's company, title, start_date, end_date, and location must be copied through \
EXACTLY unchanged - do not reorder the employers, add one, or remove one.
3. For each experience entry's bullets, select 1-4 of the strongest bullets FOR THIS ROLE from that \
employer's "available_bullets" pool below (provided verbatim from Devanshu's real past resumes) - copy \
your chosen bullets verbatim or with only light wording tweaks that change no number, no metric, and no \
named tool/technology. Do not invent a new bullet. CRITICAL: every bullet you output must be a complete, \
grammatically finished sentence or clause - never cut a bullet off mid-phrase (e.g. ending on "a", "the", \
"of", "that", "and"). If you want a shorter bullet, either copy one verbatim from the pool or rewrite it \
as a genuinely complete shorter sentence - never truncate.
4. Rewrite "summary" to foreground this role's focus, using only facts/skills that appear elsewhere in \
this document (no new claims).
5. For "skills", select and order items relevant to this role from "available_skills" below - do not add \
any skill not in that list. Keep the same category structure or a sensible subset of it.
6. Output ONLY valid JSON with this exact top-level shape: {{"contact": ..., "summary": ..., \
"experience": [...], "education": ..., "skills": {{...}}}}. No markdown fences, no commentary.

ROLE: {role_label}
Target titles this resume should read as a strong match for: {titles}

CONTACT (copy exactly):
{contact}

EDUCATION (copy exactly):
{education}

EXPERIENCE - for each employer, company/title/dates/location are FIXED (copy exactly); choose bullets \
only from that employer's available_bullets pool:
{experience_with_pools}

AVAILABLE SKILLS (pick a relevant subset, grouped however makes sense, add no new items):
{available_skills}
"""


def _format_experience_with_pools(master: dict[str, Any], library: dict[str, Any]) -> str:
    parts = []
    for e in master.get("experience", []):
        key = normalize_company(e["company"])
        pool = [b["text"] for b in library.get("bullets", {}).get(key, [])]
        parts.append(
            f"- Company: {e['company']}\n  Title: {e['title']}\n  Dates: {e['start_date']} - {e['end_date']}\n"
            f"  Location: {e.get('location', '')}\n  available_bullets:\n" +
            "\n".join(f"    * {b}" for b in pool)
        )
    return "\n\n".join(parts)


def build_prompt(role_key: str, role_spec: dict[str, Any], master: dict[str, Any], library: dict[str, Any]) -> str:
    titles = ", ".join(role_spec.get("titles_exact", []) + role_spec.get("titles_related", []))
    return PROMPT_TEMPLATE.format(
        role_label=role_key.replace("_", " ").title(),
        titles=titles,
        contact=json.dumps(master["contact"]),
        education=json.dumps(master["education"]),
        experience_with_pools=_format_experience_with_pools(master, library),
        available_skills=json.dumps(library.get("skills", {})),
    )


def generate_variant(role_key: str, master: dict[str, Any], library: dict[str, Any],
                     boundary: FactBoundary) -> dict[str, Any]:
    role_spec = config.search()["roles"][role_key]
    prompt = build_prompt(role_key, role_spec, master, library)

    for attempt in range(2):
        p = prompt if attempt == 0 else prompt + "\n\nYour previous attempt failed validation with these " \
            f"errors - fix them:\n" + "\n".join(f"- {e}" for e in last_errors)
        try:
            candidate = claude_cli.call_json(p, purpose=f"resume_variant_{role_key}", timeout=180)
        except claude_cli.ClaudeUnavailable as e:
            logger.warning("variant %s: claude unavailable (%s), falling back to master.json", role_key, e)
            return dict(master)
        except claude_cli.ClaudeBadOutput as e:
            logger.warning("variant %s attempt %d: bad output (%s)", role_key, attempt, e)
            last_errors = [f"output was not valid JSON: {e}"]
            continue
        candidate = normalize_resume_text(candidate)
        result = validate_resume(candidate, boundary)
        if result.ok:
            return candidate
        logger.warning("variant %s attempt %d failed validation: %s", role_key, attempt, result.errors)
        last_errors = result.errors

    logger.warning("variant %s: both attempts failed validation, falling back to master.json", role_key)
    return dict(master)


def generate_all_variants() -> dict[str, dict[str, Any]]:
    master = json.loads((config.RESUMES_DIR / "master.json").read_text())
    library = json.loads((config.RESUMES_DIR / "experience_library.json").read_text())
    boundary = build_fact_boundary(master, library)

    VARIANTS_DIR.mkdir(parents=True, exist_ok=True)
    results = {}
    for role_key in ("product_design", "design_engineer", "ux_design"):
        variant = generate_variant(role_key, master, library, boundary)
        (VARIANTS_DIR / f"{role_key}.json").write_text(json.dumps(variant, indent=2))
        results[role_key] = variant
        logger.info("wrote variant %s", role_key)
    return results
