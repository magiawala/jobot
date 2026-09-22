from __future__ import annotations

import hashlib
import html as htmlmod
import re
from dataclasses import dataclass, field, asdict
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .. import log

logger = log.get("discover")

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) jobbot/0.1"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


@dataclass
class Job:
    source_ats: str
    company: str
    board_token: str
    external_id: str
    title: str
    url: str
    apply_url: str
    location: str = ""
    location_all: str = ""
    country_hint: str = ""
    remote: int = 0
    workplace_type: str = ""
    description_text: str = ""
    posted_at: str | None = None
    source: str = "board"
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_source: str | None = None
    salary_interval: str | None = None
    department: str = ""
    employment_type: str = ""
    content_hash: str = field(default="")

    def finalize(self) -> "Job":
        self.title = clean_ws(self.title)
        self.location = clean_ws(self.location)
        self.location_all = clean_ws(self.location_all) or self.location
        self.description_text = self.description_text.strip()
        h = hashlib.sha256()
        h.update((self.title + "\n" + self.location_all + "\n" + self.description_text).encode("utf-8", "ignore"))
        self.content_hash = h.hexdigest()[:24]
        return self

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def clean_ws(s: str | None) -> str:
    return re.sub(r"[ \t ]+", " ", (s or "").replace("\r", "")).strip()


def strip_html(raw: str | None) -> str:
    if not raw:
        return ""
    txt = htmlmod.unescape(raw)
    if "<" in txt and ">" in txt:
        soup = BeautifulSoup(txt, "html.parser")
        for br in soup.find_all(["br"]):
            br.replace_with("\n")
        for tag in soup.find_all(["p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "ul", "ol"]):
            tag.insert_before("\n")
            tag.insert_after("\n")
        txt = soup.get_text()
    txt = htmlmod.unescape(txt)
    txt = re.sub(r"[ \t ]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n\n", txt)
    return txt.strip()


class Fetcher:
    ats: str = ""

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                                             timeout=TIMEOUT, follow_redirects=True)

    def fetch(self, company: str, board_token: str) -> list[Job]:
        raise NotImplementedError

    def _get_json(self, url: str) -> Any:
        r = self.client.get(url)
        r.raise_for_status()
        return r.json()


class BoardNotFound(Exception):
    pass
