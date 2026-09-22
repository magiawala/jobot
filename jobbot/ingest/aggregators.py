"""Phase 1b aggregator ingestion: currently just Y Combinator's public jobs board.

YC's /jobs/role/designer page is server-rendered HTML (no JS needed) and its robots.txt
only disallows "/companies?*" query strings, not "/jobs/role/*" - checked 2026-09-21.
Every sampled posting's "Apply" link routes through workatastartup.com (requires a YC
account), never directly to the company's own ATS. Per the spec, those go straight to the
"check manually" list; we only try resolve-to-ATS as a cheap safety net in case a company
happens to also be independently reachable on Greenhouse/Lever/Ashby.
"""
from __future__ import annotations

import html as htmlmod
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .. import config, db, log
from ..discover.base import USER_AGENT, clean_ws
from .resolve_ats import resolve_and_register

logger = log.get("ingest.yc")

YC_BASE = "https://www.ycombinator.com"
YC_DESIGNER_URL = f"{YC_BASE}/jobs/role/designer"

_SALARY_RE = re.compile(r"([£$€₹])\s?([\d.,]+)\s?([KMkm])?\s?(?:-|–)\s?([£$€₹])?\s?([\d.,]+)\s?([KMkm])?\s?([A-Z]{3})?")
_CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "₹": "INR"}
# Only unambiguous 2-letter country codes here - deliberately excludes ones that collide with US state
# abbreviations (CA=California, IN=Indiana, DE=Delaware, CO=Colorado, IL=Illinois/Israel, ID=Idaho/Indonesia,
# OR=Oregon, MT=Montana...). Those are instead caught via full country/city names below.
NON_US_LOCATION_WORDS = re.compile(
    r"\b(GB|UK|SG|AE|AU|NL|IE|PL|PT|MX|BR|JP|CN|HK|PH|VN|AR|CL|ZA|NG|KE|EG|TR|IT|SE|"
    r"NO|DK|FI|CH|AT|BE|RO|UA|GR|NZ|MY|TH|PK|BD|RU|India|Indonesia|Indonesian|Israel|Delaware|Colorado|"
    r"Illinois|Idaho|Colombia|Canada|Germany|Bengaluru|London|Toronto|Vancouver|Berlin|Munich|"
    r"Paris|Singapore|Dubai|Sydney|Melbourne|Amsterdam|Dublin|Warsaw|Lisbon|Mexico City|Sao Paulo|São Paulo|"
    r"Bogota|Tokyo|Beijing|Shanghai|Hong Kong|Manila|Jakarta|Gurugram|Karnataka|Hyderabad|Mumbai|Delhi|Pune|"
    r"Chennai|Kolkata|Noida|Telangana|Maharashtra)\b"
)


@dataclass
class YCListing:
    company: str
    title: str
    detail_url: str
    apply_url: str
    location: str
    salary_text: str
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    visa: str = ""


def _amount(raw: str, suffix: str | None) -> float:
    v = float(raw.replace(",", ""))
    if suffix and suffix.lower() == "k":
        v *= 1_000
    elif suffix and suffix.lower() == "m":
        v *= 1_000_000
    return v


def parse_salary(text: str) -> tuple[float | None, float | None, str | None]:
    m = _SALARY_RE.search(text)
    if not m:
        return None, None, None
    sym1, n1, suf1, sym2, n2, suf2, cur3 = m.groups()
    lo = _amount(n1, suf1)
    hi = _amount(n2, suf2 or suf1)
    currency = cur3 or _CURRENCY_SYMBOLS.get(sym1) or _CURRENCY_SYMBOLS.get(sym2 or "") or "USD"
    return lo, hi, currency


def _extract_job_postings_json(page_html: str) -> list[dict[str, Any]]:
    """YC embeds the listings as an HTML-entity-escaped JSON array under the 'jobPostings' key
    in a server-rendered data blob. No JS execution needed - just bracket-match and unescape."""
    key = "jobPostings&quot;:["
    start_key = page_html.find(key)
    if start_key == -1:
        key = '"jobPostings":['
        start_key = page_html.find(key)
        if start_key == -1:
            return []
        escaped = False
    else:
        escaped = True
    start = start_key + len(key) - 1  # position of the opening '['
    depth = 0
    end = None
    for idx in range(start, len(page_html)):
        c = page_html[idx]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end = idx + 1
                break
    if end is None:
        return []
    raw = page_html[start:end]
    if escaped:
        raw = htmlmod.unescape(raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("failed to parse YC jobPostings JSON: %s", e)
        return []


def looks_us(location: str) -> bool:
    if not location:
        return False
    if NON_US_LOCATION_WORDS.search(location):
        # allow if a clear US marker is also present (e.g. "US / CA / Remote (US; CA)")
        if re.search(r"\bUS\b|\bUnited States\b|\bU\.S\.\b", location):
            return True
        return False
    return True


def fetch_listings(client: httpx.Client) -> list[YCListing]:
    r = client.get(YC_DESIGNER_URL, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    postings = _extract_job_postings_json(r.text)
    out: list[YCListing] = []
    for j in postings:
        href = j.get("url") or ""
        salary_text = j.get("salaryRange") or ""
        lo, hi, cur = parse_salary(salary_text)
        out.append(YCListing(
            company=clean_ws(j.get("companyName") or "?"),
            title=clean_ws(j.get("title") or ""),
            detail_url=YC_BASE + href if href.startswith("/") else href,
            apply_url=j.get("applyUrl") or j.get("ctaUrl") or "",
            location=clean_ws(j.get("location") or ""),
            salary_text=salary_text, salary_min=lo, salary_max=hi, salary_currency=cur,
            visa=clean_ws(j.get("visa") or ""),
        ))
    seen: set[str] = set()
    uniq = []
    for l in out:
        if l.detail_url not in seen:
            seen.add(l.detail_url)
            uniq.append(l)
    return uniq


def resolve_apply_link(client: httpx.Client, detail_url: str) -> str | None:
    """Return the first Greenhouse/Lever/Ashby link on the job detail page, if any.
    (Safety net only - every sampled YC posting routes through workatastartup.com.)"""
    try:
        r = client.get(detail_url, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        logger.warning("could not fetch YC detail page %s: %s", detail_url, e)
        return None
    for pat in (r"https?://boards\.greenhouse\.io/[a-z0-9_-]+[^\"'\s]*",
               r"https?://job-boards\.greenhouse\.io/[a-z0-9_-]+[^\"'\s]*",
               r"https?://jobs\.lever\.co/[a-z0-9_-]+[^\"'\s]*",
               r"https?://jobs\.ashbyhq\.com/[a-z0-9_.-]+[^\"'\s]*"):
        m = re.search(pat, r.text, re.I)
        if m:
            return m.group(0)
    return None


def run_yc_ingest(salary_min_target: int = 130_000, salary_max_target: int = 200_000) -> dict[str, Any]:
    src_cfg = next((s for s in config.sources().get("sources", []) if s["id"] == "ycombinator_designer"), None)
    if not src_cfg or not src_cfg.get("enabled", True):
        return {"skipped": "source disabled"}

    counts = {"listings": 0, "us_candidates": 0, "resolved_to_ats": 0, "check_manually": 0}
    with httpx.Client(timeout=20, follow_redirects=True) as client, db.session() as conn:
        try:
            listings = fetch_listings(client)
        except Exception as e:  # noqa: BLE001
            logger.error("YC fetch failed: %s", e)
            return {"error": str(e)}
        counts["listings"] = len(listings)

        for l in listings:
            if not looks_us(l.location):
                continue
            if l.salary_min is not None and (l.salary_currency or "USD") == "USD":
                if l.salary_min > salary_max_target or (l.salary_max or l.salary_min) < salary_min_target:
                    continue
            elif l.salary_currency and l.salary_currency != "USD":
                continue
            counts["us_candidates"] += 1

            from ..discover import detect_from_text
            apply_link = l.apply_url if detect_from_text(l.apply_url) else resolve_apply_link(client, l.detail_url)
            resolved = resolve_and_register(l.company, apply_link, client) if apply_link and detect_from_text(apply_link) else None
            if resolved:
                counts["resolved_to_ats"] += 1
                logger.info("YC: resolved %s -> %s/%s", l.company, resolved["ats"], resolved["board_token"])
            else:
                added = db.add_check_manually(
                    conn, source="ycombinator", company=l.company, title=l.title, url=l.detail_url,
                    reason=f"applies via Work at a Startup (YC account required); salary={l.salary_text or 'unlisted'}; location={l.location}",
                )
                if added:
                    counts["check_manually"] += 1
            time.sleep(0.5)  # be a polite client even for read-only aggregator pages
        conn.commit()
    return counts
