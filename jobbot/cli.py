from __future__ import annotations

import json
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


if __name__ == "__main__":
    app()
