from __future__ import annotations

from datetime import datetime, timezone

import httpx

from .base import Fetcher, Job, BoardNotFound, strip_html, clean_ws

API = "https://api.lever.co/v0/postings/{token}?mode=json"


class LeverFetcher(Fetcher):
    ats = "lever"

    def fetch(self, company: str, board_token: str) -> list[Job]:
        try:
            data = self._get_json(API.format(token=board_token))
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise BoardNotFound(board_token) from e
            raise
        out: list[Job] = []
        for j in data or []:
            cats = j.get("categories") or {}
            loc = clean_ws(cats.get("location"))
            all_locs = [clean_ws(x) for x in (cats.get("allLocations") or []) if x] or ([loc] if loc else [])
            parts = [j.get("openingPlain") or "", j.get("descriptionPlain") or ""]
            for lst in j.get("lists") or []:
                parts.append((lst.get("text") or "") + "\n" + strip_html(lst.get("content")))
            parts.append(j.get("additionalPlain") or "")
            parts.append(j.get("salaryDescriptionPlain") or "")
            desc = "\n\n".join(p.strip() for p in parts if p and p.strip())
            wp = (j.get("workplaceType") or "").lower()
            created = j.get("createdAt")
            posted = datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat(timespec="seconds") if created else None
            job = Job(
                source_ats=self.ats,
                company=company,
                board_token=board_token,
                external_id=str(j["id"]),
                title=j.get("text") or "",
                url=j.get("hostedUrl") or "",
                apply_url=j.get("applyUrl") or (j.get("hostedUrl") or "") + "/apply",
                location=loc,
                location_all=" | ".join(all_locs),
                country_hint=(j.get("country") or "").upper(),
                remote=int(wp == "remote" or "remote" in loc.lower()),
                workplace_type=wp,
                description_text=desc,
                posted_at=posted,
                department=clean_ws(cats.get("department") or cats.get("team")),
                employment_type=clean_ws(cats.get("commitment")),
            )
            sr = j.get("salaryRange") or {}
            if sr.get("min") or sr.get("max"):
                job.salary_min = float(sr.get("min") or 0)
                job.salary_max = float(sr.get("max") or 0)
                job.salary_currency = (sr.get("currency") or "USD").upper()
                interval = (sr.get("interval") or "").lower()
                job.salary_interval = "hour" if "hour" in interval else "year"
                job.salary_source = "structured"
            out.append(job.finalize())
        return out
