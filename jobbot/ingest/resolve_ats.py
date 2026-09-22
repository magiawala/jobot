"""Shared 'resolve to ATS' strategy used by every aggregator/email source (Phase 1b).

Given a company name and/or an outbound link found on an aggregator listing:
1. If the link itself points at Greenhouse/Lever/Ashby, extract the board token directly.
2. Otherwise probe likely slugs for the company name against all three ATS APIs.
3. If found, add the company to companies.yaml (marked source: auto) so the next normal
   `jobbot discover` run picks up this and every other posting on that board.
4. If nothing resolves, the caller should log it to `check_manually` instead.
"""
from __future__ import annotations

import httpx

from .. import config, log
from ..discover import detect_from_text, probe_board, slug_guesses

logger = log.get("resolve_ats")


def resolve_company_to_board(company_name: str, outbound_url: str | None = None,
                             client: httpx.Client | None = None) -> dict[str, str | int] | None:
    """Returns {"ats", "board_token", "jobs"} if resolved, else None. Does not mutate config."""
    own = client is None
    client = client or httpx.Client(timeout=15, headers={"User-Agent": "jobbot/0.1"}, follow_redirects=True)
    try:
        candidates: list[tuple[str, str]] = []
        if outbound_url:
            candidates += detect_from_text(outbound_url)
        if not candidates:
            for g in slug_guesses(company_name):
                for ats in ("greenhouse", "lever", "ashby"):
                    candidates.append((ats, g))
        checked: set[tuple[str, str]] = set()
        for ats, tok in candidates:
            if (ats, tok) in checked:
                continue
            checked.add((ats, tok))
            n = probe_board(ats, tok, client)
            if n is not None:
                return {"ats": ats, "board_token": tok, "jobs": n}
        return None
    finally:
        if own:
            client.close()


def add_company_auto(name: str, ats: str, board_token: str) -> bool:
    """Add a company to companies.yaml with source: auto. Returns True if newly added."""
    cfg = config.companies()
    comps = cfg.setdefault("companies", [])
    for c in comps:
        if c["ats"] == ats and c["board_token"].lower() == board_token.lower():
            return False
    comps.append({"name": name, "ats": ats, "board_token": board_token, "source": "auto"})
    config.save_companies(cfg)
    logger.info("auto-added company %s (%s/%s)", name, ats, board_token)
    return True


def resolve_and_register(company_name: str, outbound_url: str | None = None,
                         client: httpx.Client | None = None) -> dict[str, str | int] | None:
    """Resolve + auto-add in one call. Returns the resolution dict, or None if unresolved."""
    res = resolve_company_to_board(company_name, outbound_url, client)
    if res:
        add_company_auto(company_name, str(res["ats"]), str(res["board_token"]))
    return res
