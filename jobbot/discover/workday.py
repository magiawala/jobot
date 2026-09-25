"""Workday job discovery.

Devanshu's experience is that Workday applications are the ones that produce callbacks, and
until now they weren't being searched at all - the database held 1496 Greenhouse, 1336 Ashby,
33 Lever and zero Workday.

Each Workday customer runs its own tenant, whose careers UI is backed by a public JSON search
endpoint:
    POST https://<tenant>.<shard>.myworkdayjobs.com/wday/cxs/<tenant>/<site>/jobs
Tenant, shard (wd1/wd5/wd12/...) and site name all vary per company, so they're configured in
companies.yaml rather than guessed.

DISCOVERY ONLY. Submitting a Workday application means creating an account with a password on
each company's tenant, which JobBot does not do. These jobs are queued for Devanshu to submit
by hand with the resume and answers already prepared.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx

from .. import log
from .base import Fetcher, Job, BoardNotFound, clean_ws, strip_html

logger = log.get("discover.workday")

PAGE_SIZE = 20
MAX_PAGES = 4
SEARCH_TERMS = ["product designer", "design engineer", "ux designer"]

_REL_DATE = re.compile(r"(\d+)\+?\s*(day|week|month)", re.I)


def parse_posted(posted_on: str | None) -> str | None:
    """Workday reports relative dates ("Posted 3 Days Ago"), never a timestamp."""
    if not posted_on:
        return None
    text = posted_on.lower()
    now = datetime.now(timezone.utc)
    if "today" in text or "just posted" in text:
        return now.date().isoformat()
    if "yesterday" in text:
        return (now - timedelta(days=1)).date().isoformat()
    m = _REL_DATE.search(text)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2).lower()
    delta = {"day": timedelta(days=n), "week": timedelta(weeks=n),
             "month": timedelta(days=30 * n)}[unit]
    return (now - delta).date().isoformat()


class WorkdayFetcher(Fetcher):
    ats = "workday"

    def fetch(self, company: str, board_token: str, *, shard: str = "wd1",
              site: str | None = None) -> list[Job]:
        tenant = board_token
        if not site:
            raise BoardNotFound(f"{tenant} (workday entry needs a `site`)")

        endpoint = f"https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        base = f"https://{tenant}.{shard}.myworkdayjobs.com/en-US/{site}"
        out: list[Job] = []
        seen: set[str] = set()

        for term in SEARCH_TERMS:
            for page in range(MAX_PAGES):
                payload = {"appliedFacets": {}, "limit": PAGE_SIZE,
                           "offset": page * PAGE_SIZE, "searchText": term}
                try:
                    r = self.client.post(endpoint, json=payload,
                                         headers={"Content-Type": "application/json"})
                    if r.status_code == 404:
                        raise BoardNotFound(f"{tenant}/{site}")
                    r.raise_for_status()
                    data = r.json()
                except BoardNotFound:
                    raise
                except (httpx.HTTPError, ValueError) as e:
                    logger.warning("workday %s: %s", company, e)
                    return out

                postings = data.get("jobPostings") or []
                for p in postings:
                    path = p.get("externalPath") or ""
                    if not path or path in seen:
                        continue
                    seen.add(path)
                    loc = clean_ws(p.get("locationsText"))
                    if not self._title_worth_fetching(p.get("title") or ""):
                        continue
                    out.append(Job(
                        source_ats=self.ats,
                        company=company,
                        board_token=tenant,
                        external_id=f"{tenant}:{path}",
                        title=p.get("title") or "",
                        url=base + path,
                        apply_url=base + path,
                        location=loc,
                        location_all=loc,
                        remote=int("remote" in loc.lower()),
                        # the search endpoint returns no description; scoring falls back to the
                        # title, and the posting page is fetched only if we ever need the body
                        description_text="",
                        posted_at=parse_posted(p.get("postedOn")),
                    ).finalize())

                if len(postings) < PAGE_SIZE:
                    break

        self._attach_descriptions(out, tenant, shard, site)
        return out

    @staticmethod
    def _title_worth_fetching(title: str) -> bool:
        """Cheap title gate applied BEFORE fetching descriptions.

        Workday's search is loose - "product designer" returns "Senior Mechanical Product Design
        Engineer" and "Graphics Designer/Art Director" - and each description is a separate HTTP
        request. Filtering on the title first keeps that to a handful per company instead of
        every result.
        """
        t = (title or "").lower()
        if not any(k in t for k in ("designer", "design engineer", "ux", "ui ", "product design")):
            return False
        return not any(k in t for k in (
            "mechanical", "electrical", "hardware", "silicon", "chip", "asic", "physical design",
            "intern", "graphics designer", "art director", "manager", "director", "architect"))

    def _attach_descriptions(self, jobs: list[Job], tenant: str, shard: str, site: str) -> None:
        """Fills in description_text, which the search endpoint omits entirely.

        Without this every Workday job scores on its title alone: of 135 discovered, 133 were
        binned as `skip` purely for having no description to match skills against.
        """
        base = f"https://{tenant}.{shard}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
        for job in jobs:
            path = job.external_id.split(":", 1)[-1]
            try:
                r = self.client.get(base + path, headers={"Accept": "application/json"})
                if r.status_code != 200:
                    continue
                info = r.json().get("jobPostingInfo") or {}
                job.description_text = strip_html(info.get("jobDescription"))
                job.finalize()
            except (httpx.HTTPError, ValueError) as e:
                logger.debug("workday description fetch failed for %s: %s", job.title[:40], e)
