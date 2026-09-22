"""Collapses duplicate job postings: the same underlying role posted as separate Greenhouse/
Ashby/Lever entries per office location (a very common pattern - same title, same company,
near-identical description, different external_id). We keep one canonical row per group,
merge every distinct location into it, and point the rest at it via duplicate_of_job_id so
they're excluded from scoring, review, and applications without losing their location data.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Any

from rapidfuzz import fuzz

from . import log

logger = log.get("dedupe")

DESCRIPTION_SIMILARITY_THRESHOLD = 92


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _merge_locations(location_alls: list[str]) -> str:
    seen: list[str] = []
    for loc in location_alls:
        for part in (loc or "").split(" | "):
            part = part.strip()
            if part and part not in seen:
                seen.append(part)
    return " | ".join(seen)


def run_dedupe(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT id, company, title, location_all, description_text, first_seen_at "
        "FROM jobs WHERE active=1 AND duplicate_of_job_id IS NULL"
    ).fetchall()

    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for r in rows:
        key = (r["company"].strip().lower(), normalize_title(r["title"]))
        groups.setdefault(key, []).append(r)

    groups_merged = 0
    jobs_marked_duplicate = 0

    for (company, title), members in groups.items():
        if len(members) < 2:
            continue
        clusters: list[list[sqlite3.Row]] = []
        for m in members:
            placed = False
            for cluster in clusters:
                rep = cluster[0]
                sim = fuzz.ratio(rep["description_text"] or "", m["description_text"] or "")
                if sim >= DESCRIPTION_SIMILARITY_THRESHOLD:
                    cluster.append(m)
                    placed = True
                    break
            if not placed:
                clusters.append([m])

        for cluster in clusters:
            if len(cluster) < 2:
                continue
            cluster_sorted = sorted(cluster, key=lambda r: r["first_seen_at"])
            canonical = cluster_sorted[0]
            merged_locations = _merge_locations([m["location_all"] for m in cluster_sorted])
            conn.execute("UPDATE jobs SET location_all=? WHERE id=?", (merged_locations, canonical["id"]))
            for m in cluster_sorted[1:]:
                conn.execute("UPDATE jobs SET duplicate_of_job_id=? WHERE id=?", (canonical["id"], m["id"]))
                jobs_marked_duplicate += 1
            groups_merged += 1
            logger.info("merged %d duplicate postings of %r @ %s into job #%d",
                       len(cluster_sorted) - 1, canonical["title"], company, canonical["id"])

    return {"groups_merged": groups_merged, "jobs_marked_duplicate": jobs_marked_duplicate}
