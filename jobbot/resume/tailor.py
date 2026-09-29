"""Per-job resume tailoring (Phase 3 step 3).

The role variants answer "which of three tracks is this?". This answers "what does THIS posting
actually ask for?" - selecting and ordering the bullets that speak to the job description's own
emphasis, and foregrounding the matching skills.

It reuses the variant machinery deliberately, including the fact boundary: the model may only
select and lightly reword bullets that already exist in Devanshu's real resumes. It cannot
invent an employer, a metric, a tool or a date. A tailored resume that flatters the posting by
making something up is worse than a generic one that is true, so anything failing validation
falls back to the role variant rather than being used.

Tailoring costs a Claude call per job, so it is rationed by tailoring_priority() to the jobs
where it should matter most.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .. import claude_cli, config, db, log
from .library import normalize_company
from .render import render_pdf
from .textclean import normalize_resume_text
from .validate import FactBoundary, build_fact_boundary, validate_resume
from .variants import VARIANTS_DIR, _format_experience_with_pools

logger = log.get("resume.tailor")

TAILORED_DIR = config.RESUMES_DIR / "tailored"

PROMPT = """Tailor this resume to ONE specific job posting. This is a selection and ordering task, \
not a writing task - you may ONLY use content given below, verbatim or lightly reworded. Never invent \
an employer, title, date, school, degree, number, metric, tool or skill.

A validator rejects any output that breaks these, so follow exactly:
1. "contact" and "education" are copied through EXACTLY unchanged.
2. Each experience entry's company, title, start_date, end_date and location are copied EXACTLY. \
Do not reorder, add or remove employers.
3. For each employer, choose bullets from that employer's available_bullets pool, ordered with the \
most relevant to THIS posting first. Copy them verbatim or with light wording tweaks that change no \
number, no metric and no named tool. Every bullet must be a complete sentence or clause - never cut one \
off mid-phrase (not ending on "a", "the", "of", "that", "and"). Never invent a bullet.
   LENGTH IS A HARD CONSTRAINT: at most {max_bullets} bullets across the WHOLE resume, and at most 3 for \
any one employer. This must stay a one-page resume for someone with 4 years of experience - a longer one \
reads as padded. Give the most bullets to the most recent and most relevant employers, and as few as one \
to older or less relevant ones. Tailoring means sharpening, not adding.
4. Rewrite "summary" (2-3 sentences) to speak to what THIS posting emphasises, using only facts that \
appear elsewhere in this document. No new claims. No "I am excited to" opener. No em-dashes.
5. For "skills", select and order from available_skills so the ones this posting names come first. \
Add nothing that is not in that list.
6. Output ONLY valid JSON: {{"contact": ..., "summary": ..., "experience": [...], "education": ..., \
"skills": {{...}}}}. No markdown fences, no commentary.

THE POSTING
Company: {company}
Title: {title}
What it asks for (first 2500 chars of the description):
{description}

CONTACT (copy exactly):
{contact}

EDUCATION (copy exactly):
{education}

EXPERIENCE - company/title/dates/location FIXED; choose bullets only from each pool:
{experience_with_pools}

AVAILABLE SKILLS (pick a relevant subset, add nothing new):
{available_skills}
"""


def tailored_paths(job: dict[str, Any]) -> tuple[Path, Path]:
    slug = re.sub(r"[^a-z0-9]+", "_", (job.get("company") or "job").lower()).strip("_")
    stem = f"{slug}_{job['id']}"
    return TAILORED_DIR / f"{stem}.json", TAILORED_DIR / f"{stem}.pdf"


def _role_variant(role_bucket: str | None) -> dict[str, Any] | None:
    if not role_bucket:
        return None
    path = VARIANTS_DIR / f"{role_bucket}.json"
    if path.exists():
        return json.loads(path.read_text())
    return None


def tailor_for_job(job: dict[str, Any], role_bucket: str | None = None) -> Path | None:
    """Writes a tailored resume PDF for this job, or returns None to use the role variant."""
    master_path = config.RESUMES_DIR / "master.json"
    library_path = config.RESUMES_DIR / "experience_library.json"
    if not (master_path.exists() and library_path.exists()):
        logger.warning("no master.json / experience_library.json - cannot tailor")
        return None

    master = json.loads(master_path.read_text())
    library = json.loads(library_path.read_text())
    boundary = build_fact_boundary(master, library)

    # Keep the tailored resume no longer than the master it came from. The first version let the
    # model pick up to 4 bullets per employer and it went from 14 bullets to 20, spilling onto a
    # second page - padding, which is the opposite of tailoring.
    master_bullets = sum(len(e.get("bullets") or []) for e in master.get("experience", []))
    max_bullets = max(10, master_bullets)

    prompt = PROMPT.format(
        company=job.get("company", ""),
        title=job.get("title", ""),
        max_bullets=max_bullets,
        description=(job.get("description_text") or "")[:2500],
        contact=json.dumps(master["contact"]),
        education=json.dumps(master["education"]),
        experience_with_pools=_format_experience_with_pools(master, library),
        available_skills=json.dumps(library.get("skills", {})),
    )

    last_errors: list[str] = []
    for attempt in range(2):
        p = prompt if attempt == 0 else (
            prompt + "\n\nYour previous attempt failed validation with these errors - fix them:\n"
            + "\n".join(f"- {e}" for e in last_errors))
        try:
            candidate = claude_cli.call_json(p, purpose="resume_tailor",
                                             job_id=job.get("id"), timeout=180)
        except claude_cli.ClaudeUnavailable as e:
            logger.info("tailor job %s: claude unavailable (%s) - using role variant",
                        job.get("id"), e)
            return None
        except claude_cli.ClaudeBadOutput as e:
            last_errors = [f"output was not valid JSON: {e}"]
            continue

        candidate = normalize_resume_text(candidate)
        result = validate_resume(candidate, boundary)
        if result.ok:
            return _write(job, candidate, role_bucket)
        logger.warning("tailor job %s attempt %d failed validation: %s",
                       job.get("id"), attempt, result.errors[:3])
        last_errors = result.errors

    # A tailored resume that invents something is worse than a true generic one.
    logger.warning("tailor job %s: validation failed twice - falling back to the role variant",
                   job.get("id"))
    return None


def _write(job: dict[str, Any], resume: dict[str, Any], role_bucket: str | None) -> Path:
    TAILORED_DIR.mkdir(parents=True, exist_ok=True)
    json_path, pdf_path = tailored_paths(job)
    json_path.write_text(json.dumps(resume, indent=2))
    render_pdf(resume, pdf_path)

    with db.session() as conn:
        conn.execute(
            """INSERT INTO resumes_used (job_id, kind, path_json, path_pdf, validator_report, created_at)
               VALUES (?,?,?,?,?,?)""",
            (job["id"], "tailored", str(json_path), str(pdf_path), "ok", db.now_iso()))
        conn.commit()
    logger.info("tailored resume for %s - %s", job.get("company"), job.get("title", "")[:40])
    return pdf_path


def tailor_due_jobs(limit: int | None = None) -> dict[str, Any]:
    """Tailors the highest-priority untailored jobs, within the daily budget.

    Priority comes from tailoring_priority(): bigger employers, a wider keyword gap to close,
    and better-paying postings earn the call before a marginal one does.
    """
    from .tailor_priority import PriorityInputs, rank_for_tailoring

    budget = limit if limit is not None else config.limits()["max_tailors_per_day"]
    with db.session() as conn:
        done_today = conn.execute(
            "SELECT COUNT(*) FROM resumes_used WHERE kind='tailored' AND date(created_at)=date('now')"
        ).fetchone()[0]
        budget = max(0, budget - done_today)
        if budget == 0:
            return {"tailored": 0, "reason": "daily tailoring budget already spent"}

        rows = conn.execute(
            """SELECT j.id, j.company, j.title, j.description_text, j.salary_max,
                      s.total, s.role_bucket, s.skills_pts,
                      COALESCE(cs.total_postings, 0) AS postings
               FROM jobs j
               JOIN scores s ON s.job_id = j.id
               LEFT JOIN applications a ON a.job_id = j.id
               LEFT JOIN company_stats cs ON cs.board_token = j.board_token
               LEFT JOIN resumes_used r ON r.job_id = j.id AND r.kind = 'tailored'
               WHERE j.active = 1 AND s.tier IN ('tailor', 'dream_review')
                 AND r.id IS NULL
                 AND (a.id IS NULL OR a.status = 'queued')
               ORDER BY s.total DESC LIMIT 60"""
        ).fetchall()

    if not rows:
        return {"tailored": 0, "reason": "nothing eligible"}

    dream = {c["name"].lower() for c in config.companies()["companies"] if c.get("dream")}
    fav = {c["name"].lower() for c in config.companies()["companies"] if c.get("favorite")}

    chosen, _rest = rank_for_tailoring(
        [PriorityInputs(job_id=r["id"],
                        skills_pts=r["skills_pts"] or 0,
                        salary_max=r["salary_max"],
                        is_dream=(r["company"] or "").lower() in dream,
                        is_favorite=(r["company"] or "").lower() in fav,
                        company_total_postings=r["postings"] or 0,
                        base_total=r["total"] or 0)
         for r in rows],
        max_tailors_today=budget,
    )
    by_id = {r["id"]: dict(r) for r in rows}

    made = 0
    for job_id in chosen:
        job = by_id.get(job_id)
        if not job:
            continue
        if tailor_for_job(job, job.get("role_bucket")):
            made += 1
    return {"tailored": made, "considered": len(rows), "budget": budget}
