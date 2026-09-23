"""Generates logs/dashboard.html - the at-a-glance view of the queue, progress and blockers.

Deliberately a local file rather than anything hosted: it embeds company names, application
screenshots and personal answers, none of which should leave the machine.
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config, db

OUT_PATH = config.LOG_DIR / "dashboard.html"


def _week_ago() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")


def gather(weekly_target: int = 100) -> dict[str, Any]:
    with db.session() as conn:
        status_counts = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM applications GROUP BY status")}
        tier_counts = {r["tier"]: r["c"] for r in conn.execute(
            "SELECT tier, COUNT(*) c FROM scores GROUP BY tier")}

        queue = [dict(r) for r in conn.execute(
            """SELECT a.id app_id, a.status, a.screenshot_path, a.filled_answers, a.last_error,
                      a.updated_at, j.company, j.title, j.url, j.source_ats, s.total
               FROM applications a JOIN jobs j ON j.id = a.job_id
               LEFT JOIN scores s ON s.job_id = j.id
               WHERE a.status IN ('filled_awaiting_review','needs_human')
               ORDER BY (a.status='filled_awaiting_review') DESC, s.total DESC LIMIT 60""")]

        submitted_week = conn.execute(
            "SELECT COUNT(*) FROM applications WHERE status='submitted' AND submitted_at >= ?",
            (_week_ago(),)).fetchone()[0]
        done_week = conn.execute(
            """SELECT COUNT(*) FROM applications
               WHERE status IN ('submitted','filled_awaiting_review') AND updated_at >= ?""",
            (_week_ago(),)).fetchone()[0]

        ready = conn.execute(
            """SELECT COUNT(*) FROM jobs j JOIN scores s ON s.job_id=j.id
               LEFT JOIN applications a ON a.job_id=j.id
               WHERE j.active=1 AND j.duplicate_of_job_id IS NULL AND s.tier!='skip' AND a.id IS NULL"""
        ).fetchone()[0]

        by_role = {r["role_bucket"] or "unmatched": r["c"] for r in conn.execute(
            """SELECT s.role_bucket, COUNT(*) c FROM applications a
               JOIN scores s ON s.job_id=a.job_id
               WHERE a.status IN ('submitted','filled_awaiting_review') GROUP BY s.role_bucket""")}

        runs = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 8")]

    from .apply.learned import stats as learned_stats
    return {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "status_counts": status_counts, "tier_counts": tier_counts, "queue": queue,
        "submitted_week": submitted_week, "done_week": done_week, "weekly_target": weekly_target,
        "ready": ready, "by_role": by_role, "runs": runs, "learned": learned_stats(),
        "mode": config.mode(),
    }


def _esc(v: Any) -> str:
    return html.escape(str(v if v is not None else ""))


def render(data: dict[str, Any]) -> str:
    pct = min(100, round(data["done_week"] / max(1, data["weekly_target"]) * 100))
    rows = []
    for q in data["queue"]:
        answers = json.loads(q["filled_answers"]) if q["filled_answers"] else {}
        unanswered = answers.get("unanswered", [])
        blockers = "".join(
            f"<li>{_esc(u)}</li>" for u in unanswered[:5]) or "<li class='ok'>nothing blocking</li>"
        shot = q["screenshot_path"]
        shot_link = (f"<a href='file://{_esc(shot)}' target='_blank'>screenshot</a>"
                     if shot else "<span class='dim'>no screenshot</span>")
        badge = ("ready" if q["status"] == "filled_awaiting_review" else "blocked")
        rows.append(f"""
        <tr>
          <td><span class="badge {badge}">{'READY' if badge=='ready' else 'BLOCKED'}</span></td>
          <td><strong>{_esc(q['title'])}</strong><br><span class="dim">{_esc(q['company'])} · {_esc(q['source_ats'])} · score {_esc(q['total'])}</span></td>
          <td><ul class="blockers">{blockers}</ul></td>
          <td><a href="{_esc(q['url'])}" target="_blank">posting</a><br>{shot_link}
              <br><code>jobbot approve {_esc(q['app_id'])}</code></td>
        </tr>""")

    run_rows = []
    for r in data["runs"]:
        c = json.loads(r["counts"]) if r["counts"] else {}
        errs = len(json.loads(r["errors"] or "[]"))
        run_rows.append(
            f"<tr><td class='dim'>{_esc((r['start'] or '')[:16].replace('T',' '))}</td>"
            f"<td>{c.get('discovered_new',0)} new</td><td>{c.get('attempted',0)} tried</td>"
            f"<td>{c.get('filled',0)} filled</td><td>{c.get('needs_human',0)} blocked</td>"
            f"<td>{c.get('submitted',0)} sent</td><td class='dim'>{errs} errors</td></tr>")

    learned = data["learned"]
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>JobBot</title>
<meta http-equiv="refresh" content="120">
<style>
 :root {{ --bg:#fbfbfa; --fg:#1a1a1a; --dim:#6b6b6b; --line:#e3e3e0; --ok:#1a7f45; --warn:#b45309; --accent:#2563eb; }}
 @media (prefers-color-scheme: dark) {{ :root {{ --bg:#191918; --fg:#eeeeec; --dim:#9a9a97; --line:#32312e; --ok:#4ade80; --warn:#fbbf24; --accent:#7aa2f7; }} }}
 * {{ box-sizing:border-box }}
 body {{ margin:0; padding:28px; background:var(--bg); color:var(--fg);
        font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; }}
 h1 {{ font-size:19px; margin:0 0 2px }} .dim {{ color:var(--dim) }}
 .sub {{ color:var(--dim); font-size:12.5px; margin-bottom:22px }}
 .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:22px }}
 .card {{ border:1px solid var(--line); border-radius:9px; padding:13px 15px; background:transparent }}
 .card .n {{ font-size:25px; font-weight:650; letter-spacing:-0.5px }}
 .card .l {{ color:var(--dim); font-size:11.5px; text-transform:uppercase; letter-spacing:.4px; margin-top:2px }}
 .bar {{ height:8px; background:var(--line); border-radius:99px; overflow:hidden; margin:10px 0 4px }}
 .bar i {{ display:block; height:100%; width:{pct}%; background:var(--ok); border-radius:99px }}
 table {{ width:100%; border-collapse:collapse; margin-bottom:26px }}
 th {{ text-align:left; font-size:11.5px; text-transform:uppercase; letter-spacing:.4px;
       color:var(--dim); border-bottom:1px solid var(--line); padding:7px 9px; font-weight:600 }}
 td {{ padding:10px 9px; border-bottom:1px solid var(--line); vertical-align:top }}
 .badge {{ font-size:10.5px; font-weight:700; padding:3px 7px; border-radius:5px; letter-spacing:.3px }}
 .badge.ready {{ background:rgba(26,127,69,.14); color:var(--ok) }}
 .badge.blocked {{ background:rgba(180,83,9,.14); color:var(--warn) }}
 ul.blockers {{ margin:0; padding-left:16px }} ul.blockers li {{ margin-bottom:2px }}
 ul.blockers li.ok {{ color:var(--ok); list-style:none; margin-left:-16px }}
 code {{ background:var(--line); padding:1.5px 5px; border-radius:4px; font-size:11.5px }}
 a {{ color:var(--accent) }} h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.5px;
      color:var(--dim); margin:26px 0 9px }}
</style></head><body>
<h1>JobBot</h1>
<div class="sub">{_esc(data['generated'])} · mode <strong>{_esc(data['mode'])}</strong> · auto-refreshes every 2 min</div>

<div class="cards">
  <div class="card"><div class="n">{data['done_week']}<span class="dim" style="font-size:15px">/{data['weekly_target']}</span></div>
    <div class="l">this week</div><div class="bar"><i></i></div></div>
  <div class="card"><div class="n">{data['status_counts'].get('filled_awaiting_review',0)}</div><div class="l">ready to send</div></div>
  <div class="card"><div class="n">{data['status_counts'].get('needs_human',0)}</div><div class="l">blocked</div></div>
  <div class="card"><div class="n">{data['status_counts'].get('submitted',0)}</div><div class="l">submitted</div></div>
  <div class="card"><div class="n">{data['ready']}</div><div class="l">scored &amp; unapplied</div></div>
  <div class="card"><div class="n">{learned['pending']}</div><div class="l">questions to teach</div></div>
</div>

{"<p class='dim'>Teach JobBot the " + str(learned['pending']) + " pending question(s) with <code>jobbot learn</code> - each one unblocks every future posting that asks it.</p>" if learned['pending'] else ""}

<h2>Queue</h2>
<table><tr><th></th><th>Role</th><th>Blockers</th><th>Actions</th></tr>
{''.join(rows) if rows else '<tr><td colspan="4" class="dim">Queue is empty.</td></tr>'}
</table>

<h2>Recent runs</h2>
<table><tr><th>When</th><th>Found</th><th>Tried</th><th>Filled</th><th>Blocked</th><th>Sent</th><th></th></tr>
{''.join(run_rows) if run_rows else '<tr><td colspan="7" class="dim">No runs yet.</td></tr>'}
</table>
</body></html>"""


def build(weekly_target: int = 100) -> Path:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render(gather(weekly_target)), encoding="utf-8")
    return OUT_PATH
