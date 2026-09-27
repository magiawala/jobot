"""Read-only IMAP access to the inbox, for matching recruiter replies to applications.

Credentials are never stored by JobBot and never entered by it. The password is read from the
JOBBOT_EMAIL_PASSWORD environment variable, which you set yourself - for Gmail that must be an
app password (a 16-character token generated at myaccount.google.com/apppasswords), not your
account password. App passwords are revocable independently, which is the point.

This only ever reads. It never sends, replies, moves, deletes or marks anything.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from typing import Iterator

from .. import config, log

logger = log.get("outcomes.mailbox")

ENV_PASSWORD = "JOBBOT_EMAIL_PASSWORD"
DEFAULT_HOST = "imap.gmail.com"


class MailboxUnavailable(Exception):
    """No credentials configured, or the server refused the connection."""


@dataclass
class Message:
    message_id: str
    from_addr: str
    from_name: str
    subject: str
    body: str
    received_at: str


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - malformed headers are common enough
        return value


def _plain_text(msg: email.message.Message) -> str:
    """Prefers text/plain; falls back to stripping tags out of text/html."""
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            if "attachment" in (part.get("Content-Disposition") or ""):
                continue
            try:
                raw = part.get_payload(decode=True) or b""
                text = raw.decode(part.get_content_charset() or "utf-8", "ignore")
            except Exception:  # noqa: BLE001
                continue
            parts.append(re.sub(r"<[^>]+>", " ", text) if ctype == "text/html" else text)
    else:
        try:
            raw = msg.get_payload(decode=True) or b""
            parts.append(raw.decode(msg.get_content_charset() or "utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            pass
    return re.sub(r"[ \t]+", " ", "\n".join(parts)).strip()[:20000]


def credentials() -> tuple[str, str, str]:
    """Returns (host, user, password). Raises if the password isn't configured."""
    profile = config.profile()
    user = (profile.get("email") or "").strip()
    password = os.environ.get(ENV_PASSWORD, "").strip()
    host = (config.search().get("email") or {}).get("imap_host") or DEFAULT_HOST
    if not user:
        raise MailboxUnavailable("no email address in profile.yaml")
    if not password:
        raise MailboxUnavailable(
            f"{ENV_PASSWORD} is not set. For Gmail, create an app password at "
            "myaccount.google.com/apppasswords and export it - JobBot never stores it.")
    return host, user, password


def fetch_recent(days: int = 30, limit: int = 400) -> Iterator[Message]:
    """Yields recent inbox messages, newest first. Read-only."""
    host, user, password = credentials()
    since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")

    try:
        conn = imaplib.IMAP4_SSL(host)
        conn.login(user, password)
    except imaplib.IMAP4.error as e:
        raise MailboxUnavailable(f"IMAP login failed: {e}") from e

    try:
        conn.select("INBOX", readonly=True)     # readonly: never alters the mailbox
        status, data = conn.search(None, f'(SINCE {since})')
        if status != "OK":
            return
        ids = (data[0] or b"").split()[-limit:]
        for num in reversed(ids):
            status, payload = conn.fetch(num, "(RFC822)")
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            msg = email.message_from_bytes(payload[0][1])
            from_hdr = _decode(msg.get("From"))
            addr = email.utils.parseaddr(from_hdr)[1].lower()
            yield Message(
                message_id=(msg.get("Message-ID") or f"no-id-{num.decode()}").strip(),
                from_addr=addr,
                from_name=email.utils.parseaddr(from_hdr)[0],
                subject=_decode(msg.get("Subject")),
                body=_plain_text(msg),
                received_at=(msg.get("Date") or ""),
            )
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        conn.logout()
