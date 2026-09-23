from __future__ import annotations

import re
import sqlite3
from typing import Any

from rapidfuzz import fuzz

from . import config, db, log

logger = log.get("score")

# ---------- location ----------

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
    "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida",
    "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska",
    "nevada", "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas",
    "utah", "vermont", "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "washington dc", "washington, d.c.", "district of columbia",
}
US_CITY_HINTS = {
    "san francisco", "new york", "nyc", "los angeles", "austin", "seattle", "boston", "chicago", "denver",
    "atlanta", "miami", "portland", "san diego", "san jose", "oakland", "sunnyvale", "mountain view",
    "palo alto", "menlo park", "redwood city", "santa monica", "santa clara", "cupertino", "brooklyn",
    "philadelphia", "houston", "dallas", "phoenix", "minneapolis", "nashville", "salt lake city", "raleigh",
    "durham", "washington dc", "washington, dc", "washington d.c.", "bellevue", "irvine", "pittsburgh",
    "columbus", "detroit", "charlotte", "orlando", "tampa", "jersey city", "arlington, va",
}
NON_US_TOKENS = {
    "india", "uk", "united kingdom", "london", "canada", "toronto", "vancouver", "germany", "berlin", "munich",
    "france", "paris", "singapore", "australia", "sydney", "melbourne", "dubai", "uae", "united arab emirates",
    "spain", "madrid", "barcelona", "netherlands", "amsterdam", "ireland", "dublin", "poland", "warsaw",
    "portugal", "lisbon", "mexico", "brazil", "sao paulo", "são paulo", "colombia", "bogota", "japan", "tokyo",
    "china", "beijing", "shanghai", "hong kong", "philippines", "manila", "vietnam", "indonesia", "jakarta",
    "argentina", "chile", "israel", "tel aviv", "south africa", "nigeria", "kenya", "egypt", "turkey", "istanbul",
    "italy", "milan", "rome", "sweden", "stockholm", "norway", "oslo", "denmark", "copenhagen", "finland",
    "helsinki", "switzerland", "zurich", "austria", "vienna", "belgium", "brussels", "romania", "bucharest",
    "ukraine", "kyiv", "greece", "athens", "new zealand", "auckland", "malaysia", "kuala lumpur", "thailand",
    "bangkok", "pakistan", "bangladesh", "russia", "moscow", "emea", "apac", "latam",
}
REMOTE_NON_US_RE = re.compile(
    r"remote[^,|]*\b(?:emea|apac|latam|uk|canada|india|europe|europ[e]?an union|eu\b|germany|australia|"
    r"united kingdom|ireland|mexico|brazil|philippines|poland|international)\b", re.I
)
REMOTE_US_RE = re.compile(r"remote[^,|]*\b(?:us|u\.s\.|usa|united states)\b|(?:us|u\.s\.|usa|united states)[^,|]*\bremote\b", re.I)


def location_check(job: sqlite3.Row) -> tuple[bool, list[str]]:
    """Returns (passes_us_gate, reasons)."""
    loc = (job["location_all"] or job["location"] or "").lower()
    country_hint = (job["country_hint"] or "").strip().lower()
    remote = bool(job["remote"])
    reasons: list[str] = []

    if country_hint and country_hint not in ("us", "usa", "united states", "united states of america"):
        return False, [f"country_hint={country_hint!r} is not US"]

    if not loc:
        # No location text at all: trust remote flag + country_hint (already passed above) or reject.
        if remote and not country_hint:
            reasons.append("remote flag set, no location text - assuming US-open (unverified)")
            return True, reasons
        return False, ["no location information to verify US eligibility"]

    for tok in NON_US_TOKENS:
        if re.search(rf"\b{re.escape(tok)}\b", loc):
            # allow if a US marker is ALSO present (multi-location postings like "US | Canada | UK")
            has_us = any(re.search(rf"\b{re.escape(s.lower())}\b", loc) for s in US_STATES) or \
                     any(n in loc for n in US_STATE_NAMES) or any(c in loc for c in US_CITY_HINTS) or \
                     re.search(r"\bunited states\b|\bus\b|\bu\.s\.\b|\busa\b", loc)
            if has_us:
                reasons.append(f"non-US token {tok!r} present but a US location is also listed")
                continue
            return False, [f"location {job['location_all']!r} contains non-US token {tok!r}"]

    if remote:
        if REMOTE_NON_US_RE.search(loc):
            return False, [f"remote role limited to non-US region: {job['location_all']!r}"]
        if REMOTE_US_RE.search(loc) or "united states" in loc or re.search(r"\bus\b|\bu\.s\.\b|\busa\b", loc):
            reasons.append("remote, explicitly US")
            return True, reasons
        # Generic "Remote" with no region qualifier and no non-US token matched above: allow (common case).
        reasons.append("remote with no region qualifier - treated as US-open")
        return True, reasons

    if any(re.search(rf"\b{re.escape(s)}\b", loc.upper()) for s in US_STATES) or \
       any(n in loc for n in US_STATE_NAMES) or any(c in loc for c in US_CITY_HINTS) or \
       re.search(r"\bunited states\b|\bu\.s\.\b|\busa\b", loc):
        reasons.append("US city/state/country found in location")
        return True, reasons

    return False, [f"could not confirm US location: {job['location_all']!r}"]


# ---------- salary ----------

SALARY_RANGE_RE = re.compile(
    r"(?:"
    r"\$\s?(\d{2,3}(?:,\d{3})?(?:\.\d+)?)\s?[kK]?\s?(?:-|–|to)\s?\$?\s?(\d{2,3}(?:,\d{3})?(?:\.\d+)?)\s?[kK]?"
    r"|"
    r"(\d{2,3}(?:\.\d+)?)\s?[kK]\s?(?:-|–|to)\s?(\d{2,3}(?:\.\d+)?)\s?[kK]"
    r")"
)
HOURLY_RE = re.compile(r"\$\s?(\d{1,3}(?:\.\d+)?)\s?(?:-|–|to)\s?\$?\s?(\d{1,3}(?:\.\d+)?)\s?(?:/|\s)?(?:hour|hr|per hour)", re.I)


def _norm_amount(raw: str) -> float:
    v = float(raw.replace(",", ""))
    return v * 1000 if v < 1000 else v  # "130k"/"130" (already stripped k) vs "130,000"


def parse_salary_from_text(text: str, hourly_to_annual: int) -> tuple[float, float, str] | None:
    if not text:
        return None
    m = HOURLY_RE.search(text)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return lo * hourly_to_annual, hi * hourly_to_annual, "text-hourly"
    m = SALARY_RANGE_RE.search(text)
    if m:
        g1, g2, g3, g4 = m.group(1), m.group(2), m.group(3), m.group(4)
        raw_lo, raw_hi = (g1, g2) if g1 is not None else (g3, g4)
        lo, hi = _norm_amount(raw_lo), _norm_amount(raw_hi)
        if hi < lo:
            lo, hi = hi, lo
        if hi > 1_000_000 or lo < 10_000:
            return None
        return lo, hi, "text"
    return None


def resolve_salary(job: sqlite3.Row, cfg: dict[str, Any]) -> dict[str, Any]:
    hourly_to_annual = cfg["salary"]["hourly_to_annual_hours"]
    if job["salary_min"] is not None and (job["salary_currency"] or "").upper() != "USD":
        return {"min": job["salary_min"], "max": job["salary_max"], "currency": job["salary_currency"], "source": "structured-non-usd"}
    if job["salary_min"] is not None:
        mn, mx = job["salary_min"], job["salary_max"] or job["salary_min"]
        if (job["salary_interval"] or "year") == "hour":
            mn, mx = mn * hourly_to_annual, mx * hourly_to_annual
        elif job["salary_interval"] == "month":
            mn, mx = mn * 12, mx * 12
        return {"min": mn, "max": mx, "currency": "USD", "source": "structured"}
    parsed = parse_salary_from_text(job["description_text"] or "", hourly_to_annual)
    if parsed:
        lo, hi, src = parsed
        return {"min": lo, "max": hi, "currency": "USD", "source": src}
    return {"min": None, "max": None, "currency": None, "source": "unlisted"}


def salary_gate(job: sqlite3.Row, cfg: dict[str, Any]) -> tuple[bool, int, dict[str, Any], list[str]]:
    """Returns (passes, penalty, salary_dict, reasons)."""
    s = cfg["salary"]
    sal = resolve_salary(job, cfg)
    reasons = []
    if sal["source"] == "structured-non-usd":
        return False, 0, sal, [f"non-USD salary ({sal['currency']}) implies non-US role"]
    if sal["min"] is None:
        if s["if_unlisted"] == "skip":
            return False, 0, sal, ["salary unlisted and if_unlisted=skip"]
        reasons.append(f"salary unlisted, applying -{s['unlisted_penalty']} penalty")
        return True, s["unlisted_penalty"], sal, reasons
    lo, hi = sal["min"], sal["max"]
    target_min, target_max = s["target_min"], s["target_max"]
    if hi >= target_min and lo <= target_max:
        if lo > target_max * 0.98:
            reasons.append(f"range {lo:.0f}-{hi:.0f} barely overlaps target - likely senior/staff")
        return True, 0, sal, reasons
    if lo > target_max:
        return False, 0, sal, [f"range {lo:.0f}-{hi:.0f} minimum exceeds target max {target_max:,} (likely senior/staff)"]
    return False, 0, sal, [f"range {lo:.0f}-{hi:.0f} does not overlap target {target_min:,}-{target_max:,}"]


# ---------- seniority / mid-level gate ----------

YEARS_RE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:-|to|–)\s*(\d{1,2})?\s*\+?\s*years?|"
    r"(\d{1,2})\s*\+\s*years?|minimum of (\d{1,2}) years?"
)


def parse_years_required(text: str) -> tuple[int, int] | None:
    if not text:
        return None
    best: tuple[int, int] | None = None
    for m in YEARS_RE.finditer(text):
        if m.group(1) and m.group(2):
            lo, hi = int(m.group(1)), int(m.group(2))
        elif m.group(1):
            lo, hi = int(m.group(1)), 99
        elif m.group(3):
            lo, hi = int(m.group(3)), 99
        elif m.group(4):
            lo, hi = int(m.group(4)), 99
        else:
            continue
        # Prefer a range appearing near the words "experience"/"years of" - heuristic: just take the first sane one.
        if 0 <= lo <= 20:
            best = (lo, hi)
            break
    return best


def seniority_gate(title: str, description: str, cfg: dict[str, Any]) -> tuple[bool, int, str, list[str]]:
    """Returns (passes, points 0-15, seniority_label, reasons)."""
    sen = cfg["seniority"]
    t = title.lower()
    reasons = []
    for w in sen["blocked_title_words"]:
        if re.search(rf"\b{re.escape(w.lower())}\b", t):
            return False, 0, w, [f"title contains blocked word {w!r}"]

    yrs = parse_years_required(description)
    min_ok, max_ok = sen["years_required"]["min_ok"], sen["years_required"]["max_ok"]
    if yrs:
        lo, hi = yrs
        if lo > max_ok or (hi < min_ok and hi != 99):
            return False, 0, f"{lo}-{hi if hi != 99 else '+'} yrs", [f"required years {lo}-{hi} outside allowed {min_ok}-{max_ok}"]

    is_soft = any(re.search(rf"\b{re.escape(w.lower())}\b", t) for w in sen["soft_title_words"])
    if is_soft:
        reasons.append("soft title word (e.g. Senior) - reduced points")
        return True, 8, "senior (soft)", reasons
    reasons.append("plain mid-level title")
    return True, 15, "mid", reasons


# ---------- title / skills / company ----------

def title_score(title: str, cfg: dict[str, Any]) -> tuple[int, str | None, list[str]]:
    t = title.lower()
    for w in cfg.get("title_blocklist", []):
        if re.search(rf"\b{re.escape(w.lower())}\b", t):
            return 0, None, [f"title blocklist hit: {w!r}"]
    best_score, best_role, reasons = 0, None, []
    for role, spec in cfg["roles"].items():
        for exact in spec.get("titles_exact", []):
            r = fuzz.token_sort_ratio(t, exact.lower())
            if r >= 85:
                pts = 40
            elif r >= 70:
                pts = int(40 * r / 100)
            else:
                pts = 0
            if pts > best_score:
                best_score, best_role = pts, role
        for rel in spec.get("titles_related", []):
            r = fuzz.token_sort_ratio(t, rel.lower())
            pts = 20 if r >= 80 else (int(20 * r / 100) if r >= 60 else 0)
            if pts > best_score:
                best_score, best_role = pts, role
    if best_role:
        reasons.append(f"best title match: role={best_role} pts={best_score}")
    else:
        reasons.append("no title match in any role bucket")
    return best_score, best_role, reasons


def compile_keyword_pattern(keyword_bank: set[str]) -> re.Pattern[str]:
    # Longest-first so a multi-word phrase wins over a shorter substring alternative.
    ordered = sorted(keyword_bank, key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(re.escape(kw) for kw in ordered) + r")\b", re.I)


def skills_score(description: str, keyword_pattern: re.Pattern[str] | None) -> tuple[int, list[tuple[str, str]], list[str]]:
    if not description or keyword_pattern is None:
        return 0, [], ["no description or empty keyword bank"]
    matched = sorted({m.lower() for m in keyword_pattern.findall(description)})
    pts = min(30, round(len(matched) * 30 / 12))  # ~12 matches -> full points
    return pts, matched, [f"{len(matched)} skill keywords matched"]


def location_score(job: sqlite3.Row, cfg: dict[str, Any]) -> tuple[int, list[str]]:
    loc = (job["location_all"] or "").lower()
    remote = bool(job["remote"])
    pts, reasons = 0, []
    if remote:
        pts += 6
        reasons.append("remote (+6)")
    cities = [c.lower() for c in cfg["locations"].get("cities", [])]
    if cities and any(c in loc for c in cities):
        pts += 4
        reasons.append("preferred city match (+4)")
    elif not remote:
        pts += 5
        reasons.append("onsite/hybrid US location (+5)")
    return min(pts, 10), reasons


def company_bonus(company_cfg: dict[str, Any] | None) -> tuple[int, list[str]]:
    if not company_cfg:
        return 0, []
    if company_cfg.get("dream"):
        return 15, ["dream company (+15)"]
    if company_cfg.get("favorite"):
        return 10, ["favorite company (+10)"]
    return 0, []


def build_keyword_bank() -> set[str]:
    kw = config.keywords()
    bank: set[str] = set()
    for cat in ("tools", "skills", "methods", "domains"):
        for canon, aliases in (kw.get(cat) or {}).items():
            bank.add(canon.lower())
            for a in aliases:
                bank.add(a.lower())
    return bank


def keyword_category_map() -> dict[str, str]:
    kw = config.keywords()
    m: dict[str, str] = {}
    for cat in ("tools", "skills", "methods", "domains"):
        singular = {"tools": "tool", "skills": "skill", "methods": "method", "domains": "domain"}[cat]
        for canon, aliases in (kw.get(cat) or {}).items():
            m[canon.lower()] = singular
            for a in aliases:
                m[a.lower()] = singular
    return m


def company_lookup() -> dict[str, dict[str, Any]]:
    comps = config.companies().get("companies") or []
    return {c["name"].lower(): c for c in comps}


def blocklisted(company: str) -> str | None:
    """Returns the matching blocklist entry, or None.

    Two lists feed this: companies.yaml `blocklist` (exact names) and search.yaml
    `company_blocklist`, which matches as a SUBSTRING so one entry covers a company's naming
    variants - "Anduril" catches both "Anduril" and "Anduril Industries" without listing each.
    """
    name = (company or "").lower()
    if name in {b.lower() for b in (config.companies().get("blocklist") or [])}:
        return company
    for entry in config.search().get("company_blocklist", []) or []:
        if entry.lower() in name:
            return entry
    return None


def score_job(job: sqlite3.Row, cfg: dict[str, Any], keyword_pattern: re.Pattern[str], cat_map: dict[str, str],
             companies_by_name: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Scores a job 0-100 against the hard gates. `_matched_keywords` is always populated
    (even for skipped jobs) so jd_keywords covers every analyzed job, not just survivors -
    that's what powers the market-trend view in the monthly report (Phase 6b)."""
    reasons: list[str] = []
    skills_pts, matched_kw, skills_reasons = skills_score(job["description_text"] or "", keyword_pattern)

    def skip(extra_reasons: list[str], role_bucket: str | None = None, **extra: Any) -> dict[str, Any]:
        return {"total": 0, "tier": "skip", "reasons": reasons + extra_reasons, "role_bucket": role_bucket,
                "_matched_keywords": matched_kw, **extra}

    hit = blocklisted(job["company"])
    if hit:
        return skip([f"company is blocklisted: {hit!r}"])

    us_ok, loc_reasons = location_check(job)
    reasons += loc_reasons
    if not us_ok:
        return skip([])

    title_pts, role_bucket, title_reasons = title_score(job["title"], cfg)
    reasons += title_reasons
    if title_pts == 0 and "blocklist hit" in " ".join(title_reasons):
        return skip([])

    sen_ok, sen_pts, sen_label, sen_reasons = seniority_gate(job["title"], job["description_text"] or "", cfg)
    reasons += sen_reasons
    if not sen_ok:
        return skip([], role_bucket=role_bucket, seniority_label=sen_label)

    sal_ok, sal_penalty, sal, sal_reasons = salary_gate(job, cfg)
    reasons += sal_reasons
    if not sal_ok:
        return skip([], role_bucket=role_bucket, salary=sal, seniority_label=sen_label)

    reasons += skills_reasons

    loc_pts, loc_score_reasons = location_score(job, cfg)
    reasons += loc_score_reasons

    comp_cfg = companies_by_name.get(job["company"].lower())
    comp_pts, comp_reasons = company_bonus(comp_cfg)
    reasons += comp_reasons
    if comp_cfg and comp_cfg.get("blocklist"):
        return skip(["company blocklisted"], role_bucket=role_bucket)

    total = max(0, title_pts + skills_pts + sen_pts + loc_pts + comp_pts - sal_penalty)
    thr = cfg["thresholds"]
    is_dream = bool(comp_cfg and comp_cfg.get("dream"))

    if total < thr["skip_below"]:
        tier = "skip"
    elif is_dream and total >= thr["skip_below"]:
        tier = "dream_review"
    elif total >= thr["tailor_at_or_above"]:
        tier = "tailor"
    else:
        tier = "variant"

    return {
        "total": total, "tier": tier, "role_bucket": role_bucket, "reasons": reasons,
        "title_pts": title_pts, "skills_pts": skills_pts, "seniority_pts": sen_pts,
        "location_pts": loc_pts, "company_bonus": comp_pts, "penalty": sal_penalty,
        "salary": sal, "seniority_label": sen_label, "_matched_keywords": matched_kw,
    }


def run_scoring(explain_top: int = 0) -> dict[str, Any]:
    cfg = config.search()
    keyword_bank = build_keyword_bank()
    keyword_pattern = compile_keyword_pattern(keyword_bank) if keyword_bank else None
    cat_map = keyword_category_map()
    companies_by_name = company_lookup()
    results = {"scored": 0, "skip": 0, "variant": 0, "tailor": 0, "dream_review": 0}
    explain: list[dict[str, Any]] = []
    with db.session() as conn:
        jobs = db.unscored_jobs(conn)
        for job in jobs:
            s = score_job(job, cfg, keyword_pattern, cat_map, companies_by_name)
            db.save_score(conn, job["id"], s)
            found = []
            for kw in s.get("_matched_keywords", []):
                found.append((kw, cat_map.get(kw, "skill")))
            if found:
                db.save_keywords(conn, job["id"], found)
            results["scored"] += 1
            results[s["tier"]] = results.get(s["tier"], 0) + 1
            explain.append({"job": dict(job), "score": s})
        conn.commit()
    explain.sort(key=lambda e: -e["score"]["total"])
    return {"counts": results, "top": explain[:explain_top] if explain_top else []}
