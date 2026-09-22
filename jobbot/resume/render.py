"""Renders a resume JSON (matching the master.json schema) to a clean, ATS-friendly single-
column PDF: Jinja2 -> HTML -> Playwright page.pdf(). No tables-for-layout, no images, real
selectable text throughout (per the spec's explicit no-other-PDF-library rule)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

from .. import config

TEMPLATE_DIR = config.RESUMES_DIR / "templates"
TEMPLATE_NAME = "resume.html.j2"


def render_html(resume: dict[str, Any]) -> str:
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    template = env.get_template(TEMPLATE_NAME)
    return template.render(**resume)


def render_pdf(resume: dict[str, Any], out_path: Path) -> Path:
    html = render_html(resume)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(path=str(out_path), format="Letter", print_background=True,
                margin={"top": "0in", "bottom": "0in", "left": "0in", "right": "0in"})
        browser.close()
    return out_path


def resume_filename(first: str, last: str, company: str | None = None) -> str:
    base = f"{first}_{last}_Resume"
    if company:
        safe_company = "".join(c for c in company if c.isalnum() or c in " -_").strip().replace(" ", "")
        base += f"_{safe_company}"
    return base + ".pdf"
