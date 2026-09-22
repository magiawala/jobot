from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  source_ats TEXT NOT NULL,
  company TEXT NOT NULL,
  board_token TEXT NOT NULL,
  external_id TEXT NOT NULL,
  title TEXT NOT NULL,
  location TEXT,
  location_all TEXT,
  country_hint TEXT,
  remote INTEGER DEFAULT 0,
  workplace_type TEXT,
  url TEXT,
  apply_url TEXT,
  description_text TEXT,
  posted_at TEXT,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT,
  content_hash TEXT,
  source TEXT DEFAULT 'board',
  salary_min REAL,
  salary_max REAL,
  salary_currency TEXT,
  salary_source TEXT,
  salary_interval TEXT,
  years_required TEXT,
  seniority_label TEXT,
  department TEXT,
  employment_type TEXT,
  active INTEGER DEFAULT 1,
  duplicate_of_job_id INTEGER REFERENCES jobs(id),
  UNIQUE(source_ats, board_token, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);

CREATE TABLE IF NOT EXISTS scores (
  job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
  total INTEGER NOT NULL,
  title_pts INTEGER, skills_pts INTEGER, seniority_pts INTEGER, location_pts INTEGER, company_bonus INTEGER,
  penalty INTEGER DEFAULT 0,
  role_bucket TEXT,
  tier TEXT NOT NULL,
  reasons TEXT,
  scored_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resumes_used (
  id INTEGER PRIMARY KEY,
  job_id INTEGER REFERENCES jobs(id),
  kind TEXT NOT NULL,
  path_json TEXT, path_pdf TEXT,
  validator_report TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
  id INTEGER PRIMARY KEY,
  job_id INTEGER UNIQUE REFERENCES jobs(id),
  status TEXT NOT NULL,
  attempts INTEGER DEFAULT 0,
  last_error TEXT,
  screenshot_path TEXT,
  filled_answers TEXT,
  resume_id INTEGER REFERENCES resumes_used(id),
  created_at TEXT NOT NULL,
  updated_at TEXT,
  submitted_at TEXT
);

CREATE TABLE IF NOT EXISTS claude_calls (
  id INTEGER PRIMARY KEY,
  timestamp TEXT NOT NULL,
  purpose TEXT NOT NULL,
  job_id INTEGER,
  success INTEGER,
  duration REAL,
  error TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  start TEXT NOT NULL,
  end TEXT,
  counts TEXT,
  errors TEXT
);

CREATE TABLE IF NOT EXISTS jd_keywords (
  job_id INTEGER REFERENCES jobs(id),
  keyword TEXT NOT NULL,
  category TEXT NOT NULL,
  found_at TEXT NOT NULL,
  PRIMARY KEY (job_id, keyword)
);

CREATE TABLE IF NOT EXISTS outcomes (
  id INTEGER PRIMARY KEY,
  application_id INTEGER REFERENCES applications(id),
  stage TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  evidence TEXT,
  confidence TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS processed_emails (
  message_id TEXT PRIMARY KEY,
  purpose TEXT,
  processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recommendations (
  id INTEGER PRIMARY KEY,
  month TEXT NOT NULL,
  text TEXT NOT NULL,
  config_change TEXT,
  status TEXT DEFAULT 'proposed',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS check_manually (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  company TEXT,
  title TEXT,
  url TEXT,
  reason TEXT,
  seen_at TEXT NOT NULL,
  UNIQUE(source, url)
);

CREATE TABLE IF NOT EXISTS board_probes (
  slug TEXT NOT NULL, ats TEXT NOT NULL, ok INTEGER, probed_at TEXT NOT NULL,
  PRIMARY KEY (slug, ats)
);

CREATE TABLE IF NOT EXISTS company_stats (
  ats TEXT NOT NULL, board_token TEXT NOT NULL,
  total_postings INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (ats, board_token)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent migrations for columns added after a DB already existed."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "duplicate_of_job_id" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN duplicate_of_job_id INTEGER REFERENCES jobs(id)")


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path or DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- jobs ----------

JOB_COLUMNS = [
    "source_ats", "company", "board_token", "external_id", "title", "location", "location_all", "country_hint",
    "remote", "workplace_type", "url", "apply_url", "description_text", "posted_at", "content_hash", "source",
    "salary_min", "salary_max", "salary_currency", "salary_source", "salary_interval", "department", "employment_type",
]


def upsert_job(conn: sqlite3.Connection, job: dict[str, Any]) -> tuple[int, bool, bool]:
    """Insert or update a normalized job. Returns (job_id, is_new, changed)."""
    row = conn.execute(
        "SELECT id, content_hash FROM jobs WHERE source_ats=? AND board_token=? AND external_id=?",
        (job["source_ats"], job["board_token"], job["external_id"]),
    ).fetchone()
    ts = now_iso()
    if row is None:
        cols = [c for c in JOB_COLUMNS if c in job]
        vals = [job[c] for c in cols]
        cols += ["first_seen_at", "last_seen_at", "active"]
        vals += [ts, ts, 1]
        cur = conn.execute(
            f"INSERT INTO jobs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals
        )
        return cur.lastrowid, True, True
    changed = row["content_hash"] != job.get("content_hash")
    if changed:
        cols = [c for c in JOB_COLUMNS if c in job and c not in ("source",)]
        sets = ",".join(f"{c}=?" for c in cols)
        conn.execute(f"UPDATE jobs SET {sets}, last_seen_at=?, active=1 WHERE id=?", [job[c] for c in cols] + [ts, row["id"]])
    else:
        conn.execute("UPDATE jobs SET last_seen_at=?, active=1 WHERE id=?", (ts, row["id"]))
    return row["id"], False, changed


def mark_inactive_missing(conn: sqlite3.Connection, source_ats: str, board_token: str, seen_external_ids: set[str]) -> int:
    rows = conn.execute("SELECT id, external_id FROM jobs WHERE source_ats=? AND board_token=? AND active=1",
                        (source_ats, board_token)).fetchall()
    gone = [r["id"] for r in rows if r["external_id"] not in seen_external_ids]
    for jid in gone:
        conn.execute("UPDATE jobs SET active=0 WHERE id=?", (jid,))
    return len(gone)


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def unscored_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT j.* FROM jobs j LEFT JOIN scores s ON s.job_id=j.id "
        "WHERE s.job_id IS NULL AND j.active=1 AND j.duplicate_of_job_id IS NULL ORDER BY j.first_seen_at DESC"
    ).fetchall()


# ---------- scores ----------

def save_score(conn: sqlite3.Connection, job_id: int, s: dict[str, Any]) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO scores
           (job_id,total,title_pts,skills_pts,seniority_pts,location_pts,company_bonus,penalty,role_bucket,tier,reasons,scored_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job_id, s["total"], s.get("title_pts", 0), s.get("skills_pts", 0), s.get("seniority_pts", 0),
         s.get("location_pts", 0), s.get("company_bonus", 0), s.get("penalty", 0), s.get("role_bucket"),
         s["tier"], json.dumps(s.get("reasons", [])), now_iso()),
    )
    if "salary" in s:
        sal = s["salary"]
        conn.execute("UPDATE jobs SET salary_min=?, salary_max=?, salary_currency=?, salary_source=?, salary_interval=?, years_required=?, seniority_label=? WHERE id=?",
                     (sal.get("min"), sal.get("max"), sal.get("currency"), sal.get("source"), sal.get("interval"),
                      s.get("years_required"), s.get("seniority_label"), job_id))


def save_keywords(conn: sqlite3.Connection, job_id: int, found: list[tuple[str, str]]) -> None:
    ts = now_iso()
    conn.executemany("INSERT OR IGNORE INTO jd_keywords (job_id, keyword, category, found_at) VALUES (?,?,?,?)",
                     [(job_id, k, c, ts) for k, c in found])


# ---------- applications ----------

def get_or_create_application(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchone()
    if row:
        return row
    ts = now_iso()
    conn.execute("INSERT INTO applications (job_id, status, created_at, updated_at) VALUES (?,?,?,?)", (job_id, "queued", ts, ts))
    return conn.execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchone()


def update_application(conn: sqlite3.Connection, app_id: int, **fields: Any) -> None:
    if "filled_answers" in fields and not isinstance(fields["filled_answers"], (str, type(None))):
        fields["filled_answers"] = json.dumps(fields["filled_answers"])
    fields["updated_at"] = now_iso()
    sets = ",".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE applications SET {sets} WHERE id=?", list(fields.values()) + [app_id])


# ---------- claude calls ----------

def log_claude_call(conn: sqlite3.Connection, purpose: str, job_id: int | None, success: bool, duration: float, error: str | None) -> None:
    conn.execute("INSERT INTO claude_calls (timestamp,purpose,job_id,success,duration,error) VALUES (?,?,?,?,?,?)",
                 (now_iso(), purpose, job_id, int(success), duration, error))


def claude_calls_today(conn: sqlite3.Connection, purpose_prefix: str | None = None) -> int:
    day = datetime.now(timezone.utc).date().isoformat()
    if purpose_prefix:
        return conn.execute("SELECT COUNT(*) FROM claude_calls WHERE substr(timestamp,1,10)=? AND purpose LIKE ?",
                            (day, purpose_prefix + "%")).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM claude_calls WHERE substr(timestamp,1,10)=?", (day,)).fetchone()[0]


# ---------- runs ----------

def start_run(conn: sqlite3.Connection) -> int:
    cur = conn.execute("INSERT INTO runs (start) VALUES (?)", (now_iso(),))
    return cur.lastrowid


def end_run(conn: sqlite3.Connection, run_id: int, counts: dict[str, Any], errors: list[str]) -> None:
    conn.execute("UPDATE runs SET end=?, counts=?, errors=? WHERE id=?", (now_iso(), json.dumps(counts), json.dumps(errors), run_id))


# ---------- misc ----------

def add_check_manually(conn: sqlite3.Connection, source: str, company: str | None, title: str | None, url: str | None, reason: str) -> bool:
    cur = conn.execute("INSERT OR IGNORE INTO check_manually (source,company,title,url,reason,seen_at) VALUES (?,?,?,?,?,?)",
                       (source, company, title, url, reason, now_iso()))
    return cur.rowcount > 0


def upsert_company_stats(conn: sqlite3.Connection, ats: str, board_token: str, total_postings: int) -> None:
    """Total open postings on a company's board - a free, real-time proxy for company size/activity
    (no LinkedIn scraping, no paid data). Used by the tailoring-priority ranking in Phase 3."""
    conn.execute(
        "INSERT INTO company_stats (ats, board_token, total_postings, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(ats, board_token) DO UPDATE SET total_postings=excluded.total_postings, updated_at=excluded.updated_at",
        (ats, board_token.lower(), total_postings, now_iso()),
    )


def company_size(conn: sqlite3.Connection, ats: str, board_token: str) -> int:
    row = conn.execute("SELECT total_postings FROM company_stats WHERE ats=? AND board_token=?",
                       (ats, board_token.lower())).fetchone()
    return row["total_postings"] if row else 0


def apps_today(conn: sqlite3.Connection, statuses: tuple[str, ...] = ("submitted", "filled_awaiting_review")) -> int:
    day = datetime.now(timezone.utc).date().isoformat()
    q = f"SELECT COUNT(*) FROM applications WHERE substr(updated_at,1,10)=? AND status IN ({','.join('?'*len(statuses))})"
    return conn.execute(q, (day, *statuses)).fetchone()[0]
