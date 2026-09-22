from __future__ import annotations

import httpx

from .base import Fetcher, Job, BoardNotFound, strip_html, clean_ws

API = "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"


class AshbyFetcher(Fetcher):
    ats = "ashby"

    def fetch(self, company: str, board_token: str) -> list[Job]:
        try:
            data = self._get_json(API.format(token=board_token))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise BoardNotFound(board_token) from e
            raise
        out: list[Job] = []
        for j in data.get("jobs", []):
            if j.get("isListed") is False:
                continue
            loc = clean_ws(j.get("location"))
            all_locs = [loc] if loc else []
            for s in j.get("secondaryLocations") or []:
                l2 = clean_ws(s.get("location"))
                if l2 and l2 not in all_locs:
                    all_locs.append(l2)
            country = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry") or ""
            desc = j.get("descriptionPlain") or strip_html(j.get("descriptionHtml"))
            wp = (j.get("workplaceType") or "").lower()
            job = Job(
                source_ats=self.ats,
                company=company,
                board_token=board_token,
                external_id=str(j["id"]),
                title=j.get("title") or "",
                url=j.get("jobUrl") or "",
                apply_url=j.get("applyUrl") or (j.get("jobUrl") or "") + "/application",
                location=loc,
                location_all=" | ".join(all_locs),
                country_hint=country,
                remote=int(bool(j.get("isRemote")) or wp == "remote"),
                workplace_type=wp,
                description_text=desc,
                posted_at=j.get("publishedAt"),
                department=clean_ws(j.get("department") or j.get("team")),
                employment_type=clean_ws(j.get("employmentType")),
            )
            comp = j.get("compensation") or {}
            for tier in comp.get("compensationTiers") or []:
                for c in tier.get("components") or []:
                    if c.get("compensationType") == "Salary" and (c.get("minValue") or c.get("maxValue")):
                        job.salary_min = float(c.get("minValue") or 0)
                        job.salary_max = float(c.get("maxValue") or 0)
                        job.salary_currency = (c.get("currencyCode") or "USD").upper()
                        interval = (c.get("interval") or "").upper()
                        job.salary_interval = "hour" if "HOUR" in interval else ("month" if "MONTH" in interval else "year")
                        job.salary_source = "structured"
                        break
                if job.salary_source:
                    break
            if not job.salary_source and comp.get("scrapeableCompensationSalarySummary"):
                job.description_text = f"Compensation: {comp['scrapeableCompensationSalarySummary']}\n\n" + job.description_text
            out.append(job.finalize())
        return out
