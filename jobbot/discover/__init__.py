from __future__ import annotations

import re
import time
from typing import Any, Callable

import httpx

from .. import config, db, log
from .ashby import AshbyFetcher
from .base import BoardNotFound, Fetcher, Job, USER_AGENT, TIMEOUT
from .greenhouse import GreenhouseFetcher
from .lever import LeverFetcher

logger = log.get("discover")

FETCHERS: dict[str, type[Fetcher]] = {
    "greenhouse": GreenhouseFetcher,
    "lever": LeverFetcher,
    "ashby": AshbyFetcher,
}

# Cheap title pre-filter so we only store jobs that could possibly be one of the three target roles.
# Everything that passes (including Senior/Staff/etc. that later fail the gates) is stored and keyword-analyzed.
DEFAULT_TITLE_FILTER = r"\b(design|designer|ux|ui|user experience|prototyp\w*|interaction|creative technologist|design technologist|design systems?)\b"


def title_filter_regex() -> re.Pattern[str]:
    pat = (config.search().get("discovery") or {}).get("title_regex") or DEFAULT_TITLE_FILTER
    return re.compile(pat, re.I)


def make_client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=TIMEOUT, follow_redirects=True)


def fetch_company(company: dict[str, Any], client: httpx.Client) -> list[Job]:
    ats = (company.get("ats") or "").lower()
    if ats not in FETCHERS:
        raise ValueError(f"unsupported ats {ats!r} for {company.get('name')}")
    return FETCHERS[ats](client).fetch(company["name"], company["board_token"])


def run_discovery(only: list[str] | None = None, progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Fetch every configured board, upsert jobs, return counts + new job ids."""
    cfg = config.companies()
    comps = cfg.get("companies") or []
    if only:
        wanted = {o.lower() for o in only}
        comps = [c for c in comps if c["name"].lower() in wanted or str(c.get("board_token", "")).lower() in wanted]
    tf = title_filter_regex()
    counts = {"boards": 0, "boards_failed": 0, "fetched": 0, "relevant": 0, "new": 0, "updated": 0, "deactivated": 0}
    new_ids: list[int] = []
    errors: list[str] = []
    started = time.time()
    with make_client() as client, db.session() as conn:
        for c in comps:
            name = c.get("name", "?")
            try:
                jobs = fetch_company(c, client)
            except BoardNotFound:
                counts["boards_failed"] += 1
                errors.append(f"{name}: board not found ({c.get('ats')}/{c.get('board_token')})")
                logger.warning("board not found: %s (%s/%s)", name, c.get("ats"), c.get("board_token"))
                continue
            except Exception as e:  # noqa: BLE001 - per-company isolation is the point
                counts["boards_failed"] += 1
                errors.append(f"{name}: {type(e).__name__}: {e}")
                logger.warning("fetch failed for %s: %s", name, e)
                continue
            counts["boards"] += 1
            counts["fetched"] += len(jobs)
            relevant = [j for j in jobs if tf.search(j.title)]
            counts["relevant"] += len(relevant)
            seen: set[str] = set()
            for j in relevant:
                seen.add(j.external_id)
                jid, is_new, changed = db.upsert_job(conn, j.as_dict())
                if is_new:
                    counts["new"] += 1
                    new_ids.append(jid)
                elif changed:
                    counts["updated"] += 1
            counts["deactivated"] += db.mark_inactive_missing(conn, c["ats"], c["board_token"], seen)
            if progress:
                progress(f"{name}: {len(jobs)} jobs, {len(relevant)} design-related")
            conn.commit()
    counts["seconds"] = round(time.time() - started, 1)
    return {"counts": counts, "new_ids": new_ids, "errors": errors}


# ---------- board detection / probing ----------

ATS_PATTERNS = {
    "greenhouse": [
        r"boards\.greenhouse\.io/([a-z0-9_-]+)",
        r"job-boards\.greenhouse\.io/([a-z0-9_-]+)",
        r"boards-api\.greenhouse\.io/v1/boards/([a-z0-9_-]+)",
        r"greenhouse\.io/embed/job_board\?for=([a-z0-9_-]+)",
        r"greenhouse\.io/embed/job_board/js\?for=([a-z0-9_-]+)",
    ],
    "lever": [
        r"jobs\.lever\.co/([a-z0-9_-]+)",
        r"api\.lever\.co/v0/postings/([a-z0-9_-]+)",
    ],
    "ashby": [
        r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)",
        r"api\.ashbyhq\.com/posting-api/job-board/([a-z0-9_.-]+)",
        r"ashbyhq\.com/([a-z0-9_.-]+)/embed",
    ],
}
_SKIP_TOKENS = {"embed", "jobs", "api", "v1", "v0", "boards", "posting-api", "job-board", "www"}


def probe_board(ats: str, token: str, client: httpx.Client | None = None) -> int | None:
    """Return number of jobs if the board exists, else None."""
    own = client is None
    client = client or make_client()
    try:
        jobs = FETCHERS[ats](client).fetch(token, token)
        return len(jobs)
    except BoardNotFound:
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("probe %s/%s failed: %s", ats, token, e)
        return None
    finally:
        if own:
            client.close()


def detect_from_text(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for ats, pats in ATS_PATTERNS.items():
        for p in pats:
            for m in re.finditer(p, text, re.I):
                tok = m.group(1).lower().strip("/")
                if tok and tok not in _SKIP_TOKENS and (ats, tok) not in found:
                    found.append((ats, tok))
    return found


def slug_guesses(company_name: str) -> list[str]:
    base = re.sub(r"[^a-z0-9 ]+", "", company_name.lower())
    base = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|the|labs|technologies|technology|ai)\b", " ", base)
    words = [w for w in base.split() if w]
    if not words:
        return []
    joined = "".join(words)
    guesses = [joined, "-".join(words), words[0], joined + "inc", joined + "hq", joined + "-inc"]
    seen: list[str] = []
    for g in guesses:
        if g and g not in seen:
            seen.append(g)
    return seen


def find_board(url_or_name: str, use_browser: bool = True) -> list[dict[str, Any]]:
    """Detect ATS + board token from a careers URL (or company name via slug guessing). Verifies via the public API."""
    results: list[dict[str, Any]] = []
    candidates: list[tuple[str, str]] = []
    with make_client() as client:
        if url_or_name.startswith("http"):
            candidates += detect_from_text(url_or_name)
            html = ""
            try:
                r = client.get(url_or_name, headers={"Accept": "text/html,*/*"})
                html = r.text
            except Exception as e:  # noqa: BLE001
                logger.warning("could not fetch %s: %s", url_or_name, e)
            candidates += detect_from_text(html)
            if not candidates and use_browser:
                try:
                    from playwright.sync_api import sync_playwright
                    with sync_playwright() as p:
                        b = p.chromium.launch(headless=True)
                        page = b.new_page(user_agent=USER_AGENT)
                        seen_urls: list[str] = []
                        page.on("request", lambda req: seen_urls.append(req.url))
                        page.goto(url_or_name, wait_until="networkidle", timeout=30000)
                        content = page.content() + "\n" + "\n".join(seen_urls)
                        for fr in page.frames:
                            content += "\n" + fr.url
                        b.close()
                    candidates += detect_from_text(content)
                except Exception as e:  # noqa: BLE001
                    logger.warning("browser detection failed: %s", e)
            m = re.search(r"https?://(?:www\.)?([a-z0-9-]+)\.", url_or_name, re.I)
            name_for_guess = m.group(1) if m else url_or_name
        else:
            name_for_guess = url_or_name
        if not candidates:
            for g in slug_guesses(name_for_guess):
                for ats in FETCHERS:
                    candidates.append((ats, g))
        checked: set[tuple[str, str]] = set()
        for ats, tok in candidates:
            if (ats, tok) in checked:
                continue
            checked.add((ats, tok))
            n = probe_board(ats, tok, client)
            if n is not None:
                results.append({"ats": ats, "board_token": tok, "jobs": n})
    return results
