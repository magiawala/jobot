"""Shared Playwright application-filling machinery for all ATS adapters.

Safety rules baked in here rather than left to each adapter:
  - CAPTCHAs are never solved or bypassed. A visible challenge => needs_human, full stop.
  - A login wall / account-creation page => needs_human.
  - A posting that 404s between discovery and applying => the job is marked closed, not failed
    (seen in practice: a Lever posting we discovered was gone by the time we opened it).
  - DRY_RUN and REVIEW never click submit. Only AUTO submits, and never for dream_review jobs.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from .. import config, log
from .answers import (
    STRICT_MATCH_KEYS, AnswerBook, build_answer_book, classify_label, derive_answer, is_required,
    normalize_label, pick_option,
)

logger = log.get("apply")

# A visible instance of any of these means a human has to take over.
CAPTCHA_FRAME_HINTS = ["recaptcha/api2/anchor", "recaptcha/enterprise/anchor", "hcaptcha.com/captcha",
                       "challenges.cloudflare.com"]
CAPTCHA_CHALLENGE_HINTS = ["recaptcha/api2/bframe", "hcaptcha.com/challenge"]
LOGIN_WALL_TEXTS = ["sign in to apply", "log in to apply", "create an account to apply",
                    "please sign in", "please log in", "sign up to continue"]
PRESUBMIT_CHECK_FAILED = "pre-submit validation could not run (blocked as a precaution)"

CLOSED_POSTING_TEXTS = ["no longer accepting applications", "this job is no longer",
                        "position has been filled", "posting is closed", "couldn't find anything here",
                        "404 error", "job posting you're looking for"]


class NeedsHuman(Exception):
    """Raised when something requires a person: CAPTCHA, login wall, unknown required question."""


class PostingClosed(Exception):
    """The posting 404'd or is no longer accepting applications."""


@dataclass
class FillResult:
    status: str                       # filled_awaiting_review | submitted | needs_human | failed | skipped
    filled: dict[str, str] = field(default_factory=dict)
    unanswered: list[str] = field(default_factory=list)
    screenshot_path: str | None = None
    error: str | None = None
    submitted: bool = False


def polite_pause(lo: float = 3.0, hi: float = 10.0) -> None:
    """Randomized pause between actions that submit data, per the spec's polite-client rule."""
    time.sleep(random.uniform(lo, hi))


def screenshot_path_for(job_id: int, suffix: str = "") -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"job{job_id}_{ts}{('_' + suffix) if suffix else ''}.png"
    return config.SCREENSHOT_DIR / name


class BaseApplyAdapter:
    ats = ""

    def __init__(self, page: Page, profile: dict[str, Any], resume_pdf: Path, mode: str = "REVIEW",
                 job: dict[str, Any] | None = None):
        self.page = page
        self.profile = profile
        self.resume_pdf = resume_pdf
        self.mode = mode.upper()
        self.job = job or {}
        self.answers: AnswerBook = build_answer_book(profile)
        self.filled: dict[str, str] = {}
        self.unanswered: list[str] = []

    # ---------- safety checks ----------

    def check_posting_open(self) -> None:
        body = (self.page.inner_text("body") or "").lower()
        for marker in CLOSED_POSTING_TEXTS:
            if marker in body:
                raise PostingClosed(marker)

    def check_login_wall(self) -> None:
        body = (self.page.inner_text("body") or "").lower()
        for marker in LOGIN_WALL_TEXTS:
            if marker in body:
                raise NeedsHuman(f"login/account wall detected: {marker!r}")

    def check_captcha(self) -> None:
        """Only an INTERACTIVE captcha blocks us. We never attempt to solve either kind.

        The distinction matters a lot: reCAPTCHA v3 is score-based and renders a passive badge
        on a huge share of Greenhouse boards - treating that as a challenge would route every
        Greenhouse job to needs_human. A v2 checkbox or an image challenge is the real signal.
        """
        # An image-challenge popup (bframe) is unambiguous.
        for frame in self.page.frames:
            url = (frame.url or "").lower()
            if any(hint in url for hint in CAPTCHA_CHALLENGE_HINTS):
                if self._challenge_frame_visible(url):
                    raise NeedsHuman("interactive CAPTCHA challenge present")

        # A v2 "I'm not a robot" checkbox lives in an anchor iframe that is NOT inside the
        # passive .grecaptcha-badge wrapper (which is what v3 renders).
        blocking = self.page.evaluate("""() => {
          const frames = Array.from(document.querySelectorAll('iframe'));
          for (const f of frames) {
            const src = (f.getAttribute('src') || '').toLowerCase();
            const isRecaptchaAnchor = src.includes('recaptcha') && src.includes('anchor');
            const isHcaptchaCheckbox = src.includes('hcaptcha.com') && src.includes('checkbox');
            if (!isRecaptchaAnchor && !isHcaptchaCheckbox) continue;
            if (f.closest('.grecaptcha-badge')) continue;   // v3 passive badge - not a challenge
            const r = f.getBoundingClientRect();
            const style = window.getComputedStyle(f);
            if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
            if (r.width >= 240 && r.height >= 60) return src;  // v2 checkbox is ~304x78
          }
          return null;
        }""")
        if blocking:
            raise NeedsHuman("interactive CAPTCHA checkbox present")

    def _challenge_frame_visible(self, frame_url: str) -> bool:
        """A bframe exists in the DOM even before it's shown; only a sized, visible one counts."""
        try:
            return bool(self.page.evaluate("""(needle) => {
              const f = Array.from(document.querySelectorAll('iframe'))
                .find(x => (x.getAttribute('src')||'').toLowerCase().includes(needle));
              if (!f) return false;
              const r = f.getBoundingClientRect();
              const style = window.getComputedStyle(f);
              if (style.visibility === 'hidden' || style.display === 'none') return false;
              return r.width > 100 && r.height > 100;
            }""", "bframe" if "bframe" in frame_url else "challenge"))
        except Exception:  # noqa: BLE001
            return False

    def run_safety_checks(self) -> None:
        self.check_posting_open()
        self.check_login_wall()
        self.check_captcha()

    # ---------- filling helpers ----------

    def fill_if_present(self, selector: str, value: str, key: str) -> bool:
        if not value:
            return False
        try:
            loc = self.page.locator(selector).first
            if loc.count() == 0 or not loc.is_visible():
                return False
            loc.fill(value)
            # Read back rather than trusting the write: autocomplete/combobox widgets routinely
            # clear or reject a typed value, and reporting it as "filled" would let AUTO mode
            # submit a form with a required field actually empty.
            try:
                if not (loc.input_value() or "").strip():
                    return False
            except Exception:  # noqa: BLE001 - non-input elements have no input_value()
                pass
            self.filled[key] = value
            self.drop_unanswered(key)
            return True
        except Exception as e:  # noqa: BLE001 - a single field failing must not abort the form
            logger.debug("could not fill %s: %s", selector, e)
            return False

    def upload_resume(self, selector: str) -> bool:
        try:
            loc = self.page.locator(selector).first
            if loc.count() == 0:
                return False
            loc.set_input_files(str(self.resume_pdf))
            self.filled["resume"] = self.resume_pdf.name
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("resume upload failed on %s: %s", selector, e)
            return False

    def answer_for_label(self, label: str) -> tuple[str | None, bool]:
        """Resolution order: profile answer -> derived-from-profile -> previously learned answer.
        Returns (answer_value, is_eeo); None means nothing could answer it."""
        key, eeo, _score = classify_label(label)
        if eeo:
            return self.answers.eeo.get(key, "decline"), True
        if key is not None:
            value = self.answers.get(key)
            if value:
                return value, False

        derived = derive_answer(label, self.profile)
        if derived:
            return derived, False

        from .learned import find_answer
        learned = find_answer(label)
        if learned:
            return learned, False
        return None, False

    def is_strict_label(self, label: str) -> bool:
        """True when picking a merely-similar dropdown option would be a misrepresentation."""
        key, _eeo, _score = classify_label(label)
        return key in STRICT_MATCH_KEYS

    def reconcile_unanswered(self) -> None:
        """Forms often expose one question as several elements (a visible combobox plus a hidden
        mirror input), so a question can get recorded unanswered by one element after another
        already filled it. Run once after fill() so order doesn't matter."""
        for key in list(self.filled.keys()):
            self.drop_unanswered(key)

    def maybe_draft(self, label: str, is_free_text: bool) -> str | None:
        """Short free-text 'why this company/role' questions are the one case the spec allows a
        Claude call for. Requires a job context, and only ever runs for free-text fields."""
        if not is_free_text or not self.job:
            return None
        from .draft import draft_answer, is_draftable

        if not is_draftable(label):
            return None
        answer = draft_answer(label, self.job, job_id=self.job.get("id"))
        if answer:
            logger.info("drafted answer for %r (%d chars)", label[:60], len(answer))
        return answer

    def record_unanswered(self, label: str, required: bool, options: list[str] | None = None,
                          kind: str = "text") -> None:
        tag = f"{'REQUIRED' if required else 'optional'}: {label.strip()[:120]}"
        if tag not in self.unanswered:
            self.unanswered.append(tag)
        # Capture it for one-time human input so the same question never blocks a second time.
        try:
            from .learned import record_pending

            record_pending(label, ats=self.ats, options=options, kind=kind, required=required,
                           company=self.job.get("company"), url=self.job.get("url"))
        except Exception as e:  # noqa: BLE001 - learning must never break an application
            logger.debug("could not record learnable question: %s", e)

    def drop_unanswered(self, label: str) -> None:
        """A later element for the same question succeeded (forms often expose one question as
        several elements), so it isn't actually unanswered. Matching is prefix-based because the
        fill and record paths truncate labels to different lengths."""
        target = (label or "").strip()
        if not target:
            return

        def same_question(entry: str) -> bool:
            text = entry.split(": ", 1)[-1].strip()
            shorter, longer = sorted((text, target), key=len)
            return bool(shorter) and longer.startswith(shorter)

        self.unanswered = [u for u in self.unanswered if not same_question(u)]

    # ---------- lifecycle ----------

    def open(self, apply_url: str) -> None:
        self.page.goto(apply_url, wait_until="domcontentloaded", timeout=45000)
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        self.run_safety_checks()

    def fill(self) -> None:
        raise NotImplementedError

    def submit(self) -> bool:
        raise NotImplementedError

    def empty_required_fields(self) -> list[str]:
        """Required fields still empty, read from the DOM right before an irreversible submit.

        The adapter's own `unanswered` list is only as good as its scanner, and a widget the
        scanner cannot see is invisible to it - Ashby's <button> yes/no toggles were exactly
        that, leaving three required questions blank while the run reported unanswered=0.

        FAILS CLOSED. If the check itself cannot run, it reports a sentinel blocker rather than
        an empty list: an earlier version returned [] on error, which the caller read as "clear
        to submit" - a safety gate that silently disables itself is worse than none.
        """
        try:
            script = (Path(__file__).parent / "js" / "empty_required.js").read_text(encoding="utf-8")
        except OSError as e:
            logger.error("pre-submit validation script missing: %s", e)
            return [PRESUBMIT_CHECK_FAILED]
        try:
            result = self.page.evaluate(script)
        except Exception as e:  # noqa: BLE001
            logger.error("pre-submit validation could not run (blocking submit): %s", e)
            return [PRESUBMIT_CHECK_FAILED]
        if not isinstance(result, list):
            logger.error("pre-submit validation returned %r (blocking submit)", type(result))
            return [PRESUBMIT_CHECK_FAILED]
        return [str(x) for x in result]

    def confirm_submitted(self) -> bool:
        """After clicking submit, look for a confirmation signal."""
        try:
            self.page.wait_for_load_state("networkidle", timeout=20000)
        except PWTimeout:
            pass
        body = (self.page.inner_text("body") or "").lower()
        markers = ["thank you", "application received", "application submitted", "we've received",
                   "thanks for applying", "successfully submitted"]
        return any(m in body for m in markers)


def apply_to_job(job: dict[str, Any], adapter_cls: type[BaseApplyAdapter], profile: dict[str, Any],
                 resume_pdf: Path, mode: str = "REVIEW", headed: bool = False,
                 allow_submit: bool = False) -> FillResult:
    """Drives one application end to end. `allow_submit` is a second gate on top of mode so an
    approval flow can re-open a form and submit it without flipping the global mode to AUTO."""
    mode = mode.upper()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        adapter = adapter_cls(page, profile, resume_pdf, mode=mode, job=job)
        shot: Path | None = None
        try:
            adapter.open(job["apply_url"])
            adapter.fill()
            adapter.reconcile_unanswered()  # ordering-independent: a question filled by any
                                            # element shouldn't still be listed as unanswered
            adapter.run_safety_checks()  # a captcha can appear only after interaction

            shot = screenshot_path_for(job["id"], "filled")
            page.screenshot(path=str(shot), full_page=True)

            should_submit = allow_submit or (mode == "AUTO")
            if mode == "DRY_RUN":
                should_submit = False
            # Dream companies always wait for a human, even in AUTO - you get one shot at these
            # and an auto-filled essay isn't worth burning Figma/Linear/Notion on.
            if should_submit and not allow_submit and job.get("tier") == "dream_review":
                logger.info("job %s is a dream company - holding for review despite AUTO", job.get("id"))
                return FillResult(status="filled_awaiting_review", filled=adapter.filled,
                                  unanswered=adapter.unanswered, screenshot_path=str(shot))
            if adapter.unanswered and any(u.startswith("REQUIRED") for u in adapter.unanswered):
                return FillResult(status="needs_human", filled=adapter.filled,
                                  unanswered=adapter.unanswered, screenshot_path=str(shot),
                                  error="unanswered required question(s)")
            if not should_submit:
                status = "filled_awaiting_review" if mode == "REVIEW" else "skipped"
                return FillResult(status=status, filled=adapter.filled, unanswered=adapter.unanswered,
                                  screenshot_path=str(shot))

            # Last gate before an irreversible action: read the page, don't trust our own list.
            empty = adapter.empty_required_fields()
            if empty:
                logger.warning("refusing to submit job %s - %d required field(s) still empty: %s",
                               job.get("id"), len(empty), "; ".join(empty[:5]))
                return FillResult(
                    status="needs_human", filled=adapter.filled,
                    unanswered=adapter.unanswered + [f"REQUIRED (empty at submit): {e}" for e in empty],
                    screenshot_path=str(shot),
                    error=f"{len(empty)} required field(s) still empty at submit time")

            polite_pause()
            adapter.submit()
            ok = adapter.confirm_submitted()
            shot_after = screenshot_path_for(job["id"], "after_submit")
            page.screenshot(path=str(shot_after), full_page=True)
            if ok:
                return FillResult(status="submitted", filled=adapter.filled, screenshot_path=str(shot_after),
                                  submitted=True)
            return FillResult(status="failed", filled=adapter.filled, screenshot_path=str(shot_after),
                              error="no submission confirmation detected")

        except PostingClosed as e:
            return FillResult(status="skipped", error=f"posting closed: {e}")
        except NeedsHuman as e:
            try:
                shot = shot or screenshot_path_for(job["id"], "needs_human")
                page.screenshot(path=str(shot), full_page=True)
            except Exception:  # noqa: BLE001
                pass
            return FillResult(status="needs_human", filled=adapter.filled, unanswered=adapter.unanswered,
                              screenshot_path=str(shot) if shot else None, error=str(e))
        except Exception as e:  # noqa: BLE001
            try:
                shot = shot or screenshot_path_for(job["id"], "error")
                page.screenshot(path=str(shot), full_page=True)
            except Exception:  # noqa: BLE001
                pass
            logger.exception("apply failed for job %s", job.get("id"))
            return FillResult(status="failed", filled=adapter.filled, screenshot_path=str(shot) if shot else None,
                              error=f"{type(e).__name__}: {e}")
        finally:
            context.close()
            browser.close()
