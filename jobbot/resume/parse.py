"""Code-only text extraction from resume PDFs/DOCX (no AI). Structuring into the JSON schema
happens later via Claude, but pulling raw text and doing similarity clustering here is free
and lets us avoid one Claude call per file when the source folder has hundreds of near-duplicate
company-tailored variants of the same handful of base resumes."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from docx import Document

from .. import log

logger = log.get("resume.parse")

SUPPORTED_EXTS = {".pdf", ".docx"}


@dataclass
class ExtractedResume:
    path: Path
    text: str
    error: str | None = None


def extract_pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n".join(pages)


def extract_docx_text(path: Path) -> str:
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                parts.append(cell.text)
    return "\n".join(parts)


def clean_extracted_text(text: str) -> str:
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_one(path: Path) -> ExtractedResume:
    try:
        if path.suffix.lower() == ".pdf":
            raw = extract_pdf_text(path)
        elif path.suffix.lower() == ".docx":
            raw = extract_docx_text(path)
        else:
            return ExtractedResume(path=path, text="", error=f"unsupported extension {path.suffix}")
        text = clean_extracted_text(raw)
        if len(text) < 50:
            return ExtractedResume(path=path, text=text, error="suspiciously short extraction (<50 chars)")
        return ExtractedResume(path=path, text=text)
    except Exception as e:  # noqa: BLE001 - one bad file shouldn't stop a 350-file batch
        logger.warning("failed to extract %s: %s", path, e)
        return ExtractedResume(path=path, text="", error=str(e))


def extract_all(root: Path) -> list[ExtractedResume]:
    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS and not p.name.startswith(".")
    )
    return [extract_one(p) for p in files]
