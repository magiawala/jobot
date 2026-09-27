"""Reply tracking: read the inbox, match recruiter mail to applications, record the outcome.

Without this the system runs blind - it can submit a hundred applications and learn nothing
about which resume variant, score band or source actually produces callbacks.

Everything here is read-only against the mailbox, and every recorded outcome keeps the evidence
phrase that produced it so a wrong classification can be found and corrected rather than
silently skewing the numbers.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from .. import config, db, log
from .classify import classify, is_positive, is_response
from .mailbox import MailboxUnavailable, fetch_recent
from .match import match_email

logger = log.get("outcomes")

# Stage ordering, so a later email can advance an application but never walk it backwards.
STAGE_RANK = {"auto_ack": 0, "rejected": 1, "screen": 2, "interview": 3, "offer": 4}


def _open_applications(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT a.id, a.submitted_at, j.company, j.title
           FROM applications a JOIN jobs j ON j.id = a.job_id
           WHERE a.status IN ('submitted', 'submitted_unconfirmed')"""
    ).fetchall()
    return [dict(r) for r in rows]


def _already_processed(conn, message_id: str) -> bool:
    return conn.execute("SELECT 1 FROM processed_emails WHERE message_id=?",
                        (message_id,)).fetchone() is not None


def sync_replies(days: int = 30, limit: int = 400) -> dict[str, Any]:
    """Reads recent mail and records any outcomes it can attribute confidently."""
    stats = {"scanned": 0, "matched": 0, "recorded": 0, "unmatched_replies": 0, "skipped": 0}

    with db.session() as conn:
        applications = _open_applications(conn)
    if not applications:
        logger.info("no submitted applications yet - nothing to match replies against")
        return stats

    try:
        messages = list(fetch_recent(days=days, limit=limit))
    except MailboxUnavailable as e:
        logger.warning("mailbox unavailable: %s", e)
        stats["error"] = str(e)
        return stats

    with db.session() as conn:
        for msg in messages:
            stats["scanned"] += 1
            if _already_processed(conn, msg.message_id):
                stats["skipped"] += 1
                continue

            result = classify(msg.subject, msg.body, msg.from_addr)
            conn.execute(
                "INSERT OR REPLACE INTO processed_emails (message_id, purpose, processed_at)"
                " VALUES (?,?,?)", (msg.message_id, f"reply:{result.stage}", db.now_iso()))

            if result.stage == "unknown" or not is_response(result.stage) and result.stage != "auto_ack":
                continue

            hit = match_email(msg.from_addr, msg.subject, msg.body, applications)
            if hit is None:
                if is_response(result.stage):
                    stats["unmatched_replies"] += 1
                    logger.info("unmatched %s reply from %s: %r",
                                result.stage, msg.from_addr, msg.subject[:60])
                continue
            stats["matched"] += 1

            # A reply from the company is proof the application actually landed - better
            # evidence than watching for a submission POST, which this project got wrong three
            # separate ways. Any matched reply, including a plain auto-acknowledgement,
            # upgrades submitted_unconfirmed to submitted.
            app_row = conn.execute("SELECT status FROM applications WHERE id=?",
                                   (hit.application_id,)).fetchone()
            if app_row and app_row["status"] == "submitted_unconfirmed":
                conn.execute(
                    """UPDATE applications SET status='submitted', submitted_at=COALESCE(submitted_at, ?),
                       last_error='confirmed by reply from the company', updated_at=?
                       WHERE id=?""",
                    (msg.received_at or db.now_iso(), db.now_iso(), hit.application_id))
                stats["confirmed_by_reply"] = stats.get("confirmed_by_reply", 0) + 1
                logger.info("confirmed %s submitted - company replied", hit.company)

            existing = conn.execute(
                "SELECT stage FROM outcomes WHERE application_id=? ORDER BY id DESC LIMIT 1",
                (hit.application_id,)).fetchone()
            if existing and STAGE_RANK.get(existing["stage"], 0) >= STAGE_RANK.get(result.stage, 0):
                continue    # never walk an application backwards down the funnel

            conn.execute(
                """INSERT INTO outcomes (application_id, stage, occurred_at, evidence, confidence, note)
                   VALUES (?,?,?,?,?,?)""",
                (hit.application_id, result.stage, msg.received_at or db.now_iso(),
                 result.evidence[:200], result.confidence,
                 f"from={msg.from_addr} subject={msg.subject[:90]} match={hit.how}"))
            stats["recorded"] += 1
            logger.info("%s -> %s (%s)", hit.company, result.stage, result.evidence[:50])
        conn.commit()

    logger.info("reply sync: %s", json.dumps(stats))
    return stats


def funnel_metrics() -> dict[str, Any]:
    """Response and positive-response rates, broken down by the things we can change.

    Groups below MIN_GROUP are reported but flagged: with a handful of applications a difference
    between variants is noise, and acting on it would be worse than not measuring at all.
    """
    min_group = 20
    out: dict[str, Any] = {"min_group": min_group}

    with db.session() as conn:
        rows = conn.execute(
            """SELECT a.id, a.filled_answers, j.source_ats, j.company, s.total, s.tier,
                      s.role_bucket, j.salary_min,
                      (SELECT stage FROM outcomes o WHERE o.application_id = a.id
                       ORDER BY id DESC LIMIT 1) AS stage
               FROM applications a
               JOIN jobs j ON j.id = a.job_id
               LEFT JOIN scores s ON s.job_id = j.id
               WHERE a.status IN ('submitted', 'submitted_unconfirmed')"""
        ).fetchall()

    def bucket_score(total: int | None) -> str:
        if total is None:
            return "unscored"
        return "90+" if total >= 90 else "75-89" if total >= 75 else "60-74" if total >= 60 else "<60"

    groups: dict[str, dict[str, list[str]]] = {
        "overall": defaultdict(list), "score_band": defaultdict(list),
        "source_ats": defaultdict(list), "role": defaultdict(list),
        "resume_kind": defaultdict(list), "salary_listed": defaultdict(list),
    }

    for r in rows:
        stage = r["stage"] or "no_reply"
        resume = "unknown"
        if r["filled_answers"]:
            try:
                path = json.loads(r["filled_answers"]).get("resume") or ""
                resume = "tailored" if "tailored" in path else (
                    path.rsplit("/", 1)[-1].replace(".pdf", "") if path else "unknown")
            except json.JSONDecodeError:
                pass
        groups["overall"]["all"].append(stage)
        groups["score_band"][bucket_score(r["total"])].append(stage)
        groups["source_ats"][r["source_ats"] or "?"].append(stage)
        groups["role"][r["role_bucket"] or "?"].append(stage)
        groups["resume_kind"][resume].append(stage)
        groups["salary_listed"]["listed" if r["salary_min"] else "unlisted"].append(stage)

    for name, buckets in groups.items():
        out[name] = {}
        for key, stages in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
            n = len(stages)
            responses = sum(1 for s in stages if is_response(s))
            positives = sum(1 for s in stages if is_positive(s))
            out[name][key] = {
                "applications": n,
                "responses": responses,
                "response_rate": round(responses / n * 100, 1) if n else 0.0,
                "positive": positives,
                "positive_rate": round(positives / n * 100, 1) if n else 0.0,
                # below this, a difference between groups is noise
                "significant": n >= min_group,
            }
    out["total_applications"] = len(rows)
    return out
