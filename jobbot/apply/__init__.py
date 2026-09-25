from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import config, db, log
from .ashby import AshbyAdapter
from .base import BaseApplyAdapter, FillResult, apply_to_job
from .greenhouse import GreenhouseAdapter
from .lever import LeverAdapter

logger = log.get("apply")

ADAPTERS: dict[str, type[BaseApplyAdapter]] = {
    "greenhouse": GreenhouseAdapter,
    "lever": LeverAdapter,
    "ashby": AshbyAdapter,
}


def resume_for_job(job: dict[str, Any]) -> Path:
    """Picks the resume PDF to attach: the tailored one if it exists, else the role variant,
    else the master. (Per-job tailoring lands in Phase 3 step 3; this already prefers it.)"""
    tailored = config.RESUMES_DIR / "tailored" / f"{job['company'].lower().replace(' ', '_')}_{job['id']}.pdf"
    if tailored.exists():
        return tailored
    with db.session() as conn:
        row = conn.execute("SELECT role_bucket FROM scores WHERE job_id=?", (job["id"],)).fetchone()
    role = row["role_bucket"] if row and row["role_bucket"] else None
    if role:
        variant = config.RESUMES_DIR / "variants" / f"{role}.pdf"
        if variant.exists():
            return variant
    return config.RESUMES_DIR / "master.pdf"


def apply_one(job_id: int, mode: str | None = None, headed: bool = False,
              allow_submit: bool = False) -> FillResult:
    mode = (mode or config.mode()).upper()
    with db.session() as conn:
        row = db.get_job(conn, job_id)
        if row is None:
            return FillResult(status="failed", error=f"job {job_id} not found")
        job = dict(row)
        # tier drives the AUTO-mode dream-company hold, so it must travel with the job
        score_row = conn.execute("SELECT tier, total, role_bucket FROM scores WHERE job_id=?",
                                 (job_id,)).fetchone()
        if score_row:
            job.update({"tier": score_row["tier"], "score": score_row["total"],
                        "role_bucket": score_row["role_bucket"]})

    resume_pdf = resume_for_job(job)
    adapter_cls = ADAPTERS.get(job["source_ats"])
    if adapter_cls is None:
        if job["source_ats"] == "workday":
            # Workday requires creating an account with a password on each company's tenant.
            # JobBot does not create accounts, so these are surfaced for manual submission with
            # the right resume already chosen rather than being retried forever.
            return FillResult(
                status="needs_human",
                filled={"resume": str(resume_pdf)},
                error="Workday needs an account per company - apply by hand; "
                      f"use {resume_pdf.name}")
        return FillResult(status="needs_human", error=f"no adapter for ATS {job['source_ats']!r}")

    if not resume_pdf.exists():
        return FillResult(status="failed", error=f"resume PDF missing: {resume_pdf}")

    profile = config.profile()
    result = apply_to_job(job, adapter_cls, profile, resume_pdf, mode=mode, headed=headed,
                          allow_submit=allow_submit)

    with db.session() as conn:
        app_row = db.get_or_create_application(conn, job_id)
        fields: dict[str, Any] = {
            "status": result.status,
            "attempts": (app_row["attempts"] or 0) + 1,
            "last_error": result.error,
            "screenshot_path": result.screenshot_path,
            "filled_answers": {"filled": result.filled, "unanswered": result.unanswered,
                               "resume": str(resume_pdf)},
        }
        if result.submitted:
            fields["submitted_at"] = db.now_iso()
        db.update_application(conn, app_row["id"], **fields)
        if result.status == "skipped" and result.error and "closed" in (result.error or ""):
            conn.execute("UPDATE jobs SET active=0 WHERE id=?", (job_id,))
        conn.commit()

    logger.info("apply job=%s status=%s filled=%d unanswered=%d", job_id, result.status,
                len(result.filled), len(result.unanswered))
    return result
