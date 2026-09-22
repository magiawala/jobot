from __future__ import annotations

import httpx

from .base import Fetcher, Job, BoardNotFound, strip_html, clean_ws

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true&pay_transparency=true"


class GreenhouseFetcher(Fetcher):
    ats = "greenhouse"

    def fetch(self, company: str, board_token: str) -> list[Job]:
        try:
            data = self._get_json(API.format(token=board_token))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise BoardNotFound(board_token) from e
            raise
        out: list[Job] = []
        for j in data.get("jobs", []):
            loc = clean_ws((j.get("location") or {}).get("name"))
            offices = [clean_ws(o.get("name")) for o in (j.get("offices") or []) if o.get("name")]
            all_locs = [p.strip() for p in loc.replace("•", "|").replace(";", "|").split("|") if p.strip()]
            for o in offices:
                if o not in all_locs:
                    all_locs.append(o)
            dept = ", ".join(d.get("name", "") for d in (j.get("departments") or []) if d.get("name"))
            job = Job(
                source_ats=self.ats,
                company=company,
                board_token=board_token,
                external_id=str(j["id"]),
                title=j.get("title") or "",
                url=j.get("absolute_url") or "",
                apply_url=j.get("absolute_url") or "",
                location=loc,
                location_all=" | ".join(all_locs),
                remote=int("remote" in loc.lower()),
                description_text=strip_html(j.get("content")),
                posted_at=j.get("first_published") or j.get("updated_at"),
                department=dept,
            )
            for pr in j.get("pay_input_ranges") or []:
                mn, mx = pr.get("min_cents"), pr.get("max_cents")
                if mn or mx:
                    job.salary_min = (mn or 0) / 100.0
                    job.salary_max = (mx or 0) / 100.0
                    job.salary_currency = pr.get("currency_type") or "USD"
                    job.salary_source = "structured"
                    title = (pr.get("title") or "").lower()
                    hourly = "hour" in title or (job.salary_max or job.salary_min) < 1000
                    job.salary_interval = "hour" if hourly else "year"
                    break
            out.append(job.finalize())
        return out
