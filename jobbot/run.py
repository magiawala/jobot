"""The hourly orchestrator: discover -> score -> assign resume -> fill (within limits) -> record.

Never raises on individual job failures - one bad posting must not kill the run, and the exit
code stays 0 so launchd doesn't mark the agent as crashed and back off.

A lock file prevents overlapping runs (the spec's requirement, and the same pattern
opencode-scheduler uses): if the previous hour's run is still going, this one exits cleanly.
"""
from __future__ import annotations

import json
import os
import signal
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config, db, log

logger = log.get("run")


@contextmanager
def run_lock() -> Iterator[bool]:
    """Yields True if we acquired the lock, False if another run holds it."""
    lock = config.LOCK_PATH
    if lock.exists():
        try:
            pid = int(lock.read_text().strip() or 0)
        except (ValueError, OSError):
            pid = 0
        if pid and _pid_alive(pid):
            logger.info("another run is active (pid %s) - exiting cleanly", pid)
            yield False
            return
        logger.warning("removing stale lock from pid %s", pid)
        lock.unlink(missing_ok=True)

    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))
    try:
        yield True
    finally:
        lock.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def selectable_jobs(conn, limit: int) -> list[dict[str, Any]]:
    """Scored jobs worth applying to that we haven't already handled, freshest first.

    Ordering is by posting age bucket BEFORE score, deliberately: being early to a posting
    moves the needle on getting a reply far more than a few points of match score do. A job
    posted today at 80 is a better use of a slot than a three-month-old one at 92, which has
    likely already got a full pipeline of candidates (or is stale and unfilled for a reason).
    Within a bucket, the best match wins.
    """
    thresholds = config.search()["thresholds"]
    rows = conn.execute(
        """SELECT j.id, j.company, j.title, j.apply_url, j.source_ats, j.posted_at,
                  s.total, s.tier,
                  CAST(julianday('now') - julianday(j.posted_at) AS INTEGER) AS age_days
           FROM jobs j
           JOIN scores s ON s.job_id = j.id
           LEFT JOIN applications a ON a.job_id = j.id
           WHERE j.active = 1
             AND j.duplicate_of_job_id IS NULL
             AND s.tier != 'skip'
             AND s.total >= ?
             AND (a.id IS NULL OR a.status = 'queued')
           ORDER BY
             CASE
               WHEN j.posted_at IS NULL THEN 4
               WHEN julianday('now') - julianday(j.posted_at) <= 3  THEN 0
               WHEN julianday('now') - julianday(j.posted_at) <= 7  THEN 1
               WHEN julianday('now') - julianday(j.posted_at) <= 21 THEN 2
               ELSE 3
             END,
             s.total DESC
           LIMIT ?""",
        (thresholds["skip_below"], limit),
    ).fetchall()
    return [dict(r) for r in rows]


def run_once(skip_discovery: bool = False, max_apps: int | None = None,
             dry_run: bool = False) -> dict[str, Any]:
    counts: dict[str, Any] = {"discovered_new": 0, "scored": 0, "attempted": 0, "filled": 0,
                              "submitted": 0, "needs_human": 0, "failed": 0, "skipped": 0}
    errors: list[str] = []
    started = time.time()
    limits = config.limits()
    mode = "DRY_RUN" if dry_run else config.mode()

    with db.session() as conn:
        run_id = db.start_run(conn)
        conn.commit()

    # ---- discover ----
    if not skip_discovery:
        try:
            from .discover import run_discovery
            res = run_discovery()
            counts["discovered_new"] = res["counts"]["new"]
            counts["duplicates_merged"] = res["counts"].get("duplicates_marked", 0)
            errors.extend(res["errors"][:20])
        except Exception as e:  # noqa: BLE001
            errors.append(f"discovery: {type(e).__name__}: {e}")
            logger.exception("discovery stage failed")

    # ---- score ----
    try:
        from .score import run_scoring
        res = run_scoring()
        counts["scored"] = res["counts"]["scored"]
    except Exception as e:  # noqa: BLE001
        errors.append(f"scoring: {type(e).__name__}: {e}")
        logger.exception("scoring stage failed")

    # ---- apply ----
    per_run = max_apps if max_apps is not None else limits["max_apps_per_run"]
    with db.session() as conn:
        today = db.apps_today(conn)
        budget = max(0, min(per_run, limits["max_apps_per_day"] - today))
        jobs = selectable_jobs(conn, budget) if budget else []
    if budget == 0:
        logger.info("daily application cap reached (%s) - not filling any more today", limits["max_apps_per_day"])

    from .apply import apply_one
    for job in jobs:
        counts["attempted"] += 1
        try:
            result = apply_one(job["id"], mode=mode)
            if result.status == "submitted":
                counts["submitted"] += 1
            elif result.status == "filled_awaiting_review":
                counts["filled"] += 1
            elif result.status == "needs_human":
                counts["needs_human"] += 1
            elif result.status == "failed":
                counts["failed"] += 1
                if result.error:
                    errors.append(f"job {job['id']} ({job['company']}): {result.error}")
            else:
                counts["skipped"] += 1
        except Exception as e:  # noqa: BLE001 - never let one posting kill the run
            counts["failed"] += 1
            errors.append(f"job {job['id']}: {type(e).__name__}: {e}")
            logger.exception("apply failed for job %s", job["id"])

    counts["seconds"] = round(time.time() - started, 1)
    counts["mode"] = mode
    with db.session() as conn:
        db.end_run(conn, run_id, counts, errors)
        conn.commit()
    logger.info("run complete: %s", json.dumps(counts))
    return {"counts": counts, "errors": errors}


def main(skip_discovery: bool = False, max_apps: int | None = None, dry_run: bool = False) -> int:
    with run_lock() as acquired:
        if not acquired:
            return 0
        warning = config.api_key_warning()
        if warning:
            logger.warning(warning)
        try:
            run_once(skip_discovery=skip_discovery, max_apps=max_apps, dry_run=dry_run)
        except Exception:  # noqa: BLE001
            logger.exception("run failed")
    return 0  # always 0: a non-zero exit makes launchd throttle the agent
