from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.console import Console
from rich.table import Table

from . import config, db, log

app = typer.Typer(help="JobBot: local automated job application pipeline.", no_args_is_help=True, add_completion=False)
console = Console()
logger = log.get("cli")


def _banner() -> None:
    w = config.api_key_warning()
    if w:
        console.print(f"[bold red]WARNING:[/] {w}")


@app.callback()
def _main() -> None:
    _banner()


# ---------------- Phase 1: discovery ----------------

@app.command()
def discover(
    only: Optional[str] = typer.Option(None, "--only", help="Comma-separated company names or board tokens to fetch."),
    samples: int = typer.Option(5, help="How many normalized records to print."),
    quiet: bool = typer.Option(False, "--quiet", "-q"),
) -> None:
    """Fetch every configured Greenhouse/Lever/Ashby board and store new design-related jobs."""
    from .discover import run_discovery

    only_list = [s.strip() for s in only.split(",")] if only else None
    res = run_discovery(only_list, progress=None if quiet else (lambda m: console.print(f"  [dim]{m}[/]")))
    c = res["counts"]
    console.print(
        f"\n[bold]Discovery:[/] {c['boards']} boards ok, {c['boards_failed']} failed | "
        f"{c['fetched']} postings fetched, {c['relevant']} design-related | "
        f"[green]{c['new']} new[/], {c['updated']} updated, {c['deactivated']} closed | "
        f"{c.get('duplicates_marked', 0)} duplicate postings merged into {c.get('duplicate_groups', 0)} groups | {c['seconds']}s"
    )
    for e in res["errors"]:
        console.print(f"  [red]error:[/] {e}")
    if samples and res["new_ids"]:
        with db.session() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs WHERE id IN ({','.join('?'*len(res['new_ids'][:samples]))})", res["new_ids"][:samples]
            ).fetchall()
        for r in rows:
            _print_job(dict(r))


def _print_job(j: dict) -> None:
    sal = ""
    if j.get("salary_min") or j.get("salary_max"):
        sal = f"{j.get('salary_currency','')} {int(j.get('salary_min') or 0):,}-{int(j.get('salary_max') or 0):,}/{j.get('salary_interval','year')} ({j.get('salary_source')})"
    desc = (j.get("description_text") or "")
    console.print(
        f"\n[bold cyan]#{j['id']}[/] [bold]{j['title']}[/] @ {j['company']} [dim]({j['source_ats']}/{j['board_token']}:{j['external_id']})[/]\n"
        f"  location: {j.get('location_all') or j.get('location')}  remote={j.get('remote')} wp={j.get('workplace_type') or '-'} country_hint={j.get('country_hint') or '-'}\n"
        f"  salary: {sal or 'unlisted'}   posted: {j.get('posted_at')}   dept: {j.get('department') or '-'}\n"
        f"  url: {j.get('url')}\n"
        f"  desc[{len(desc)} chars]: {desc[:220].replace(chr(10),' ')}..."
    )


@app.command()
def dedupe() -> None:
    """Re-run duplicate-posting detection (same company+title+near-identical description,
    filed as separate per-location postings). Runs automatically after every discover too."""
    from .dedupe import run_dedupe

    with db.session() as conn:
        res = run_dedupe(conn)
        conn.commit()
    console.print(f"[bold]Dedupe:[/] merged {res['jobs_marked_duplicate']} duplicate postings into {res['groups_merged']} groups")


@app.command("find-board")
def find_board_cmd(url_or_name: str = typer.Argument(..., help="Careers page URL or a company name."),
                   no_browser: bool = typer.Option(False, "--no-browser")) -> None:
    """Detect whether a careers page is Greenhouse, Lever, or Ashby and print the board token."""
    from .discover import find_board

    res = find_board(url_or_name, use_browser=not no_browser)
    if not res:
        console.print("[yellow]No Greenhouse/Lever/Ashby board detected.[/] Add it to companies.yaml under `unsupported`.")
        raise typer.Exit(1)
    for r in res:
        console.print(f"[green]{r['ats']}[/]  board_token: [bold]{r['board_token']}[/]  ({r['jobs']} live postings)")
        console.print(f"  yaml: - {{ name: <Name>, ats: {r['ats']}, board_token: {r['board_token']} }}")


@app.command()
def jobs(limit: int = 20, all_jobs: bool = typer.Option(False, "--all", help="Include closed and duplicate postings.")) -> None:
    """List recently discovered jobs (duplicates hidden by default)."""
    with db.session() as conn:
        q = "SELECT j.*, s.total, s.tier FROM jobs j LEFT JOIN scores s ON s.job_id=j.id"
        if not all_jobs:
            q += " WHERE j.active=1 AND j.duplicate_of_job_id IS NULL"
        rows = conn.execute(q + " ORDER BY j.first_seen_at DESC LIMIT ?", (limit,)).fetchall()
    t = Table(box=box.SIMPLE_HEAD)
    for col in ("id", "company", "title", "location", "salary", "score", "tier", "seen"):
        t.add_column(col)
    for r in rows:
        sal = f"{int(r['salary_min'] or 0)//1000}-{int(r['salary_max'] or 0)//1000}K" if r["salary_min"] else "-"
        t.add_row(str(r["id"]), r["company"], r["title"][:48], (r["location"] or "")[:28], sal,
                  str(r["total"]) if r["total"] is not None else "-", r["tier"] or "-", r["first_seen_at"][:10])
    console.print(t)


@app.command("ingest-yc")
def ingest_yc() -> None:
    """Phase 1b: fetch Y Combinator's public designer-jobs page, filter to US + salary range,
    try to resolve each company to Greenhouse/Lever/Ashby, and log the rest to check-manually."""
    from .ingest.aggregators import run_yc_ingest

    res = run_yc_ingest()
    if "error" in res:
        console.print(f"[red]YC ingest failed:[/] {res['error']}")
        raise typer.Exit(1)
    if "skipped" in res:
        console.print(f"[yellow]YC ingest skipped:[/] {res['skipped']}")
        return
    console.print(
        f"[bold]YC ingest:[/] {res['listings']} listings | {res['us_candidates']} US+salary candidates | "
        f"[green]{res['resolved_to_ats']} resolved to ATS[/] | {res['check_manually']} added to check-manually"
    )


@app.command()
def score(explain: bool = typer.Option(False, "--explain", help="Print the top 15 new jobs with score breakdowns.")) -> None:
    """Score every unscored job against search.yaml (hard gates + 0-100 scoring, no AI)."""
    from .score import run_scoring

    res = run_scoring(explain_top=15 if explain else 0)
    c = res["counts"]
    console.print(
        f"[bold]Scoring:[/] {c['scored']} scored | "
        f"[red]skip={c.get('skip',0)}[/] variant={c.get('variant',0)} "
        f"[green]tailor={c.get('tailor',0)}[/] [magenta]dream_review={c.get('dream_review',0)}[/]"
    )
    for e in res["top"]:
        j, s = e["job"], e["score"]
        tier_color = {"skip": "red", "variant": "yellow", "tailor": "green", "dream_review": "magenta"}.get(s["tier"], "white")
        console.print(
            f"\n[bold cyan]#{j['id']}[/] [bold]{j['title']}[/] @ {j['company']}  "
            f"[bold {tier_color}]{s['tier']}[/] total={s['total']} "
            f"(title={s.get('title_pts','-')} skills={s.get('skills_pts','-')} sen={s.get('seniority_pts','-')} "
            f"loc={s.get('location_pts','-')} co={s.get('company_bonus','-')} pen=-{s.get('penalty',0)}) "
            f"role={s.get('role_bucket') or '-'}"
        )
        for r in s.get("reasons", []):
            console.print(f"    [dim]- {r}[/]")


@app.command("run")
def run_cmd(
    skip_discovery: bool = typer.Option(False, "--skip-discovery", help="Reuse already-discovered jobs."),
    max_apps: Optional[int] = typer.Option(None, help="Override max applications this run."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fill forms but never submit, whatever the config says."),
) -> None:
    """One full pipeline pass: discover -> score -> fill (within limits). This is what the
    hourly schedule calls."""
    from .run import run_once, run_lock

    with run_lock() as acquired:
        if not acquired:
            console.print("[yellow]Another run is active - exiting.[/]")
            return
        res = run_once(skip_discovery=skip_discovery, max_apps=max_apps, dry_run=dry_run)
    c = res["counts"]
    console.print(
        f"[bold]Run ({c.get('mode')}):[/] {c['discovered_new']} new jobs, {c['scored']} scored | "
        f"attempted {c['attempted']} -> [cyan]{c['filled']} filled[/], [green]{c['submitted']} submitted[/], "
        f"[yellow]{c['needs_human']} needs human[/], [red]{c['failed']} failed[/], {c['skipped']} skipped | "
        f"{c['seconds']}s"
    )
    for e in res["errors"][:10]:
        console.print(f"  [red]{e}[/]")
    if c["filled"]:
        console.print("\n[dim]jobbot review   # see what's waiting for approval[/]")


@app.command("install-schedule")
def install_schedule(report_hour: Optional[int] = typer.Option(None, help="Hour (0-23) for the daily report.")) -> None:
    """Install the hourly run + daily report as launchd agents."""
    if config.detect_os() != "macos":
        console.print(f"[red]Only macOS is supported so far (detected {config.detect_os()}).[/]")
        raise typer.Exit(1)
    from .scheduler.macos import install, sleep_settings_advice

    written = install(report_hour=report_hour)
    for p in written:
        console.print(f"[green]installed[/] {p}")
    console.print(f"\n{sleep_settings_advice()}")
    console.print("\n[dim]jobbot status   # confirm both agents are loaded[/]")


@app.command("uninstall-schedule")
def uninstall_schedule() -> None:
    """Remove the launchd agents."""
    from .scheduler.macos import uninstall

    removed = uninstall()
    console.print(f"removed: {', '.join(removed) if removed else 'nothing was installed'}")


@app.command("apply")
def apply_cmd(
    job_id: int = typer.Argument(..., help="Job id from `jobbot jobs`."),
    mode: Optional[str] = typer.Option(None, help="DRY_RUN | REVIEW | AUTO (defaults to search.yaml)."),
    headed: bool = typer.Option(False, "--headed", help="Show the browser (debugging)."),
) -> None:
    """Fill one job's application form. DRY_RUN and REVIEW never click submit."""
    from .apply import apply_one

    res = apply_one(job_id, mode=mode, headed=headed)
    color = {"submitted": "green", "filled_awaiting_review": "cyan", "needs_human": "yellow",
             "failed": "red", "skipped": "dim"}.get(res.status, "white")
    console.print(f"[bold {color}]{res.status}[/] job #{job_id}")
    if res.error:
        console.print(f"  [red]{res.error}[/]")
    if res.filled:
        console.print(f"  [bold]filled {len(res.filled)} field(s):[/]")
        for k, v in res.filled.items():
            console.print(f"    [dim]{k}[/] = {str(v)[:70]}")
    for u in res.unanswered:
        console.print(f"  [yellow]unanswered[/] {u}")
    if res.screenshot_path:
        console.print(f"  [dim]screenshot: {res.screenshot_path}[/]")


@app.command()
def dashboard(
    target: int = typer.Option(100, help="Weekly application target to track against."),
    open_it: bool = typer.Option(True, "--open/--no-open", help="Open it in your browser."),
) -> None:
    """Build logs/dashboard.html - the queue, weekly progress, blockers and recent runs."""
    import subprocess

    from .dashboard import build

    path = build(weekly_target=target)
    console.print(f"[green]built[/] {path}")
    if open_it:
        subprocess.run(["open", str(path)], capture_output=True)


@app.command()
def learn(
    interactive: bool = typer.Option(True, "--interactive/--list", help="Prompt for each, or just list."),
) -> None:
    """Answer the questions that blocked applications. Each one you answer here is reused
    automatically on every future posting that asks it, so needs_human shrinks over time."""
    from .apply.learned import LEARNED_PATH, load, promote_answered, save, stats

    promoted = promote_answered()
    if promoted:
        console.print(f"[green]{promoted}[/] previously-filled answer(s) moved into active use.")

    data = load()
    pending = sorted(data["pending"], key=lambda e: (-int(e.get("seen_count", 1)), not e.get("required")))
    s = stats()
    console.print(f"[bold]Learned answers:[/] {s['answered']} active, {s['pending']} pending "
                  f"({s['pending_blocking']} blocking applications)\n")
    if not pending:
        console.print("Nothing pending. [dim]" + str(LEARNED_PATH) + "[/]")
        return

    if not interactive:
        for e in pending:
            req = "[red]REQUIRED[/]" if e.get("required") else "optional"
            console.print(f"{req} [dim]seen {e.get('seen_count',1)}x on {e.get('ats')}[/]\n  {e['question']}")
            if e.get("options"):
                console.print(f"  [dim]options: {', '.join(e['options'][:8])}[/]")
        console.print(f"\n[dim]Edit {LEARNED_PATH} directly, or run `jobbot learn` to answer here.[/]")
        return

    answered = 0
    for e in pending:
        req = "[red]REQUIRED[/]" if e.get("required") else "[dim]optional[/]"
        console.print(f"\n{req} seen [bold]{e.get('seen_count',1)}x[/] on {e.get('ats')} "
                      f"(e.g. {e.get('example_company') or '?'})")
        console.print(f"[bold]{e['question']}[/]")
        if e.get("options"):
            console.print("  choices: " + " | ".join(e["options"][:12]))
        reply = typer.prompt("  your answer (blank to skip)", default="", show_default=False)
        if reply.strip():
            e["answer"] = reply.strip()
            answered += 1
    save(data)
    moved = promote_answered()
    console.print(f"\n[green]Saved {answered} answer(s)[/]; {moved} now active for future applications.")


@app.command()
def review() -> None:
    """List applications filled and waiting for your approval."""
    with db.session() as conn:
        rows = conn.execute(
            """SELECT a.id, a.job_id, a.status, a.screenshot_path, a.filled_answers, a.last_error,
                      j.company, j.title, j.url, s.total
               FROM applications a JOIN jobs j ON j.id=a.job_id
               LEFT JOIN scores s ON s.job_id=j.id
               WHERE a.status IN ('filled_awaiting_review','needs_human')
               ORDER BY s.total DESC"""
        ).fetchall()
    if not rows:
        console.print("Nothing awaiting review.")
        return
    for r in rows:
        tag = "[cyan]awaiting review[/]" if r["status"] == "filled_awaiting_review" else "[yellow]needs human[/]"
        console.print(f"\n{tag} [bold]app #{r['id']}[/] (job #{r['job_id']}, score {r['total']})")
        console.print(f"  [bold]{r['title']}[/] @ {r['company']}")
        console.print(f"  {r['url']}")
        if r["last_error"]:
            console.print(f"  [red]{r['last_error']}[/]")
        if r["filled_answers"]:
            data = json.loads(r["filled_answers"])
            for u in data.get("unanswered", []):
                console.print(f"  [yellow]unanswered[/] {u}")
            console.print(f"  [dim]resume: {data.get('resume','?')}[/]")
        if r["screenshot_path"]:
            console.print(f"  [dim]screenshot: {r['screenshot_path']}[/]")
    console.print("\n[dim]jobbot approve <id>   jobbot reject <id>[/]")


@app.command()
def approve(app_id: str = typer.Argument(..., help="Application id, or 'all'.")) -> None:
    """Re-open a reviewed application and actually submit it."""
    from .apply import apply_one

    with db.session() as conn:
        if app_id == "all":
            rows = conn.execute("SELECT id, job_id FROM applications WHERE status='filled_awaiting_review'").fetchall()
        else:
            rows = conn.execute("SELECT id, job_id FROM applications WHERE id=? AND status='filled_awaiting_review'",
                               (int(app_id),)).fetchall()
    if not rows:
        console.print("[yellow]Nothing to approve with that id.[/]")
        raise typer.Exit(1)

    for r in rows:
        console.print(f"Submitting application #{r['id']} (job #{r['job_id']})...")
        res = apply_one(r["job_id"], allow_submit=True)
        color = "green" if res.status == "submitted" else "red"
        console.print(f"  [bold {color}]{res.status}[/]" + (f" - {res.error}" if res.error else ""))


@app.command()
def reject(app_id: int = typer.Argument(..., help="Application id to skip.")) -> None:
    """Mark a reviewed application as skipped - it won't be submitted or retried."""
    with db.session() as conn:
        row = conn.execute("SELECT id FROM applications WHERE id=?", (app_id,)).fetchone()
        if not row:
            console.print("[yellow]No such application.[/]")
            raise typer.Exit(1)
        db.update_application(conn, app_id, status="skipped", last_error="rejected by user")
        conn.commit()
    console.print(f"[dim]Application #{app_id} marked skipped.[/]")


@app.command()
def requeue(
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt."),
) -> None:
    """Re-queue applications whose submission could never be verified.

    These had submit clicked but produced no confirmation we could trust. Re-queuing risks a
    duplicate application where one did land; leaving them risks losing the posting entirely.
    Confirmed submissions are never touched.
    """
    with db.session() as conn:
        rows = conn.execute(
            """SELECT a.id, a.job_id, j.company, j.title FROM applications a
               JOIN jobs j ON j.id = a.job_id
               WHERE a.status = 'submitted_unconfirmed' ORDER BY j.company"""
        ).fetchall()

    if not rows:
        console.print("Nothing to re-queue.")
        return

    console.print(f"[bold]{len(rows)}[/] unverifiable application(s) would be re-queued:")
    for r in rows[:12]:
        console.print(f"   [dim]{r['company']}[/] - {r['title'][:46]}")
    if len(rows) > 12:
        console.print(f"   [dim]... and {len(rows) - 12} more[/]")
    console.print("\n[yellow]If any of these did submit, re-running creates a duplicate "
                  "application.[/] Most ATSs de-duplicate by email address.")

    if not yes and not typer.confirm("Re-queue them?", default=False):
        console.print("[dim]Left as they were.[/]")
        return

    with db.session() as conn:
        for r in rows:
            db.update_application(conn, r["id"], status="queued", submitted_at=None,
                                  last_error="re-queued: earlier attempt could not be verified")
        conn.commit()
    console.print(f"[green]Re-queued {len(rows)}[/]; the hourly run will work through them "
                  "within the daily cap.")


@app.command()
def status() -> None:
    """Last run, today's counts, Claude CLI reachability."""
    from .claude_cli import check_cli
    with db.session() as conn:
        last = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        n_jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        n_active = conn.execute("SELECT COUNT(*) FROM jobs WHERE active=1 AND duplicate_of_job_id IS NULL").fetchone()[0]
        n_dupes = conn.execute("SELECT COUNT(*) FROM jobs WHERE duplicate_of_job_id IS NOT NULL").fetchone()[0]
        n_scored = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
        tiers = conn.execute("SELECT tier, COUNT(*) c FROM scores GROUP BY tier").fetchall()
        apps = conn.execute("SELECT status, COUNT(*) c FROM applications GROUP BY status").fetchall()
        calls = db.claude_calls_today(conn)
    console.print(f"[bold]OS:[/] {config.detect_os()}   [bold]mode:[/] {config.mode()}")
    console.print(f"[bold]last run:[/] {dict(last) if last else 'never'}")
    console.print(f"[bold]jobs:[/] {n_jobs} total, {n_active} active ({n_dupes} duplicates hidden), {n_scored} scored  " + " ".join(f"{t['tier']}={t['c']}" for t in tiers))
    console.print(f"[bold]applications:[/] " + (" ".join(f"{a['status']}={a['c']}" for a in apps) or "none"))
    console.print(f"[bold]claude calls today:[/] {calls}/{config.limits()['max_claude_calls_per_day']}   [bold]claude cli:[/] {check_cli()}")
    from .scheduler import schedule_status
    console.print(f"[bold]schedule:[/] {schedule_status()}")

    # The failure mode that would otherwise be invisible: launchd runs with its own PATH, so a
    # `claude` that works in your shell can still be missing inside the scheduled run.
    if config.detect_os() == "macos":
        from .scheduler.macos import LABEL_HOURLY, _launchd_path_value, plist_path
        import plistlib
        p = plist_path(LABEL_HOURLY)
        if p.exists():
            with open(p, "rb") as f:
                scheduled_path = plistlib.load(f).get("EnvironmentVariables", {}).get("PATH", "")
            from .claude_cli import binary
            claude_bin = binary()
            ok = bool(claude_bin) and any(str(Path(claude_bin).parent) == d for d in scheduled_path.split(":"))
            if ok:
                console.print("[bold]scheduled PATH:[/] [green]includes claude[/]")
            else:
                console.print(f"[bold]scheduled PATH:[/] [red]does NOT include claude ({claude_bin})[/] "
                             "- re-run `jobbot install-schedule`")

    profile_gaps = [k for k, v in (config.profile().get("work_authorization") or {}).items() if v is None]
    if profile_gaps:
        console.print(f"[yellow]profile: work_authorization unset ({', '.join(profile_gaps)}) - "
                      "those jobs route to needs_human[/]")


if __name__ == "__main__":
    app()
