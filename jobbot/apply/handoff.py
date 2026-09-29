"""Fills forms that JobBot cannot submit, and hands them to Devanshu at the last click.

Greenhouse boards using reCAPTCHA Enterprise gate the submission itself - the form fills
perfectly and the button is enabled, but the request either returns 428 or is never sent. That
gate is there to confirm a human is submitting, so it gets a human rather than a workaround.

What this removes is the boring part: every field, every drafted essay and the right resume are
already in place. What's left is the verification code and one click, about ten seconds a job,
several jobs at a time in one visible browser window.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from .. import config, db, log
from . import ADAPTERS, resume_for_job
from .base import NeedsHuman, PostingClosed, screenshot_path_for

logger = log.get("apply.handoff")


@dataclass
class Prepared:
    job_id: int
    company: str
    title: str
    url: str
    filled: int
    blockers: list[str]
    error: str | None = None


def candidates(limit: int, ats: str | None = None) -> list[dict[str, Any]]:
    """Jobs worth a hand-off: the ones the bot cannot submit itself, freshest first.

    Defaults to EXCLUDING the automatable ATSs. Handing over an Ashby job would waste the
    scarcer resource - Devanshu's attention - on something the hourly run submits unattended.
    """
    exclude = [] if ats else (config.search().get("automatable_ats") or ["ashby", "lever"])
    with db.session() as conn:
        rows = conn.execute(
            """SELECT j.id, j.company, j.title, j.apply_url, j.source_ats, s.total,
                      CAST(julianday('now') - julianday(j.posted_at) AS INTEGER) AS age_days
               FROM jobs j
               JOIN scores s ON s.job_id = j.id
               LEFT JOIN applications a ON a.job_id = j.id
               WHERE j.active = 1 AND j.duplicate_of_job_id IS NULL AND s.tier != 'skip'
                 AND (? IS NULL OR j.source_ats = ?)
                 AND (j.source_ats NOT IN (SELECT value FROM json_each(?)))
                 AND (a.id IS NULL OR a.status IN ('queued', 'needs_human'))
                 AND COALESCE(a.status, '') != 'submitted'
               ORDER BY
                 CASE WHEN julianday('now') - julianday(j.posted_at) <= 3 THEN 0
                      WHEN julianday('now') - julianday(j.posted_at) <= 7 THEN 1 ELSE 2 END,
                 s.total DESC
               LIMIT ?""",
            (ats, ats, __import__("json").dumps(exclude), limit),
        ).fetchall()
    return [dict(r) for r in rows]


def prepare_batch(jobs: list[dict[str, Any]], pause: bool = True) -> list[Prepared]:
    """Opens each job in its own tab of one visible window, fills it, and leaves it open.

    The browser stays open until you press Enter in the terminal, so you can work through the
    tabs at your own pace. Nothing here clicks submit - that is the whole point.
    """
    profile = config.profile()
    prepared: list[Prepared] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--start-maximized"])
        context = browser.new_context(viewport=None)

        for job in jobs:
            adapter_cls = ADAPTERS.get(job["source_ats"])
            if adapter_cls is None:
                continue
            resume = resume_for_job(job)
            page = context.new_page()
            try:
                adapter = adapter_cls(page, profile, resume, mode="REVIEW", job=job)
                # skip the checks that would abort: the CAPTCHA is exactly why we're here, and
                # a long form is still worth filling if a human is going to finish it anyway
                page.goto(job["apply_url"], wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:  # noqa: BLE001
                    pass
                adapter.wait_for_form()
                adapter.check_posting_open()
                adapter.fill()
                adapter.reconcile_unanswered()

                empty = adapter.empty_required_fields()
                shot = screenshot_path_for(job["id"], "handoff")
                page.screenshot(path=str(shot), full_page=True)
                prepared.append(Prepared(job["id"], job["company"], job["title"],
                                         job["apply_url"], len(adapter.filled), empty))

                with db.session() as conn:
                    app = db.get_or_create_application(conn, job["id"])
                    db.update_application(
                        conn, app["id"], status="awaiting_manual_submit",
                        screenshot_path=str(shot),
                        last_error="filled and handed off - enter the code and click submit",
                        filled_answers={"filled": adapter.filled,
                                        "unanswered": adapter.unanswered,
                                        "resume": str(resume)})
                    conn.commit()
                logger.info("prepared %s - %s (%d fields)", job["company"], job["title"][:40],
                            len(adapter.filled))

            except PostingClosed as e:
                prepared.append(Prepared(job["id"], job["company"], job["title"],
                                         job["apply_url"], 0, [], f"posting closed: {e}"))
                page.close()
            except NeedsHuman as e:
                prepared.append(Prepared(job["id"], job["company"], job["title"],
                                         job["apply_url"], 0, [], str(e)))
            except Exception as e:  # noqa: BLE001 - one bad form must not lose the batch
                logger.exception("handoff prep failed for job %s", job["id"])
                prepared.append(Prepared(job["id"], job["company"], job["title"],
                                         job["apply_url"], 0, [], f"{type(e).__name__}: {e}"))

        if pause and prepared:
            print("\n" + "=" * 74)
            print("  Forms are filled and open in the browser, one per tab.")
            print("  For each: enter the verification code if asked, then click Submit.")
            print("  Nothing has been submitted for you.")
            print("=" * 74)
            try:
                input("\n  Press Enter here when you're done to close the browser... ")
            except (EOFError, KeyboardInterrupt):
                pass

        context.close()
        browser.close()
    return prepared


def mark_submitted(job_ids: list[int]) -> int:
    """Records the ones you actually submitted, so they aren't offered again."""
    done = 0
    with db.session() as conn:
        for jid in job_ids:
            row = conn.execute("SELECT id FROM applications WHERE job_id=?", (jid,)).fetchone()
            if not row:
                continue
            db.update_application(conn, row["id"], status="submitted",
                                  submitted_at=db.now_iso(),
                                  last_error="submitted by hand after hand-off")
            done += 1
        conn.commit()
    return done
