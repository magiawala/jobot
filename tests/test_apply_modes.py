"""Mode gating: what may and may not be submitted automatically.

These guard an irreversible action - you cannot un-apply to a job - so they're worth pinning
down explicitly rather than trusting the branch reads correctly.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jobbot.apply import base


class StubAdapter(base.BaseApplyAdapter):
    """Fills nothing, reports whatever unanswered list the test supplies, records submit()."""
    ats = "stub"

    def __init__(self, *args, unanswered=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._unanswered = unanswered or []
        self.submitted = False

    def fill(self) -> None:
        self.filled = {"email": "x@example.com"}
        self.unanswered = list(self._unanswered)

    def run_safety_checks(self) -> None:
        return None

    def open(self, apply_url: str) -> None:
        return None

    def submit(self) -> bool:
        self.submitted = True
        return True

    def confirm_submitted(self) -> bool:
        return True


@pytest.fixture
def patched(monkeypatch, tmp_path):
    """Replaces Playwright with stubs so mode logic can be tested without a browser."""
    created = {}

    class FakePage:
        def screenshot(self, **kw):
            Path(kw["path"]).write_bytes(b"")

        def evaluate(self, *a, **kw):
            # stands in for the pre-submit DOM scan: no empty required fields
            return []

    class FakeContext:
        def new_page(self):
            return FakePage()
        def close(self):
            pass

    class FakeBrowser:
        def new_context(self, **kw):
            return FakeContext()
        def close(self):
            pass

    class FakeChromium:
        def launch(self, **kw):
            return FakeBrowser()

    class FakePW:
        chromium = FakeChromium()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(base, "sync_playwright", lambda: FakePW())
    monkeypatch.setattr(base, "polite_pause", lambda *a, **k: None)
    monkeypatch.setattr(base.config, "SCREENSHOT_DIR", tmp_path)

    def make(tier="tailor", unanswered=None):
        job = {"id": 1, "apply_url": "https://example.com/apply", "company": "Acme", "tier": tier}

        def factory(page, profile, resume_pdf, mode="REVIEW", job=None):
            adapter = StubAdapter(page, profile, resume_pdf, mode=mode, job=job,
                                  unanswered=unanswered)
            created["adapter"] = adapter
            return adapter

        return job, factory, created

    return make


def test_dry_run_never_submits(patched):
    job, factory, created = patched()
    result = base.apply_to_job(job, factory, {}, Path("r.pdf"), mode="DRY_RUN")
    assert created["adapter"].submitted is False
    assert result.submitted is False


def test_review_never_submits_and_queues_for_approval(patched):
    job, factory, created = patched()
    result = base.apply_to_job(job, factory, {}, Path("r.pdf"), mode="REVIEW")
    assert created["adapter"].submitted is False
    assert result.status == "filled_awaiting_review"


def test_auto_submits_a_normal_job(patched):
    job, factory, created = patched(tier="tailor")
    result = base.apply_to_job(job, factory, {}, Path("r.pdf"), mode="AUTO")
    assert created["adapter"].submitted is True
    assert result.status == "submitted"


def test_auto_holds_dream_companies_for_review(patched):
    """You get one shot at a dream company; AUTO must not spend it unattended."""
    job, factory, created = patched(tier="dream_review")
    result = base.apply_to_job(job, factory, {}, Path("r.pdf"), mode="AUTO")
    assert created["adapter"].submitted is False
    assert result.status == "filled_awaiting_review"


def test_auto_refuses_when_a_required_question_is_unanswered(patched):
    job, factory, created = patched(unanswered=["REQUIRED: Export control question"])
    result = base.apply_to_job(job, factory, {}, Path("r.pdf"), mode="AUTO")
    assert created["adapter"].submitted is False
    assert result.status == "needs_human"


def test_explicit_approval_can_submit_a_dream_company(patched):
    """`jobbot approve` passes allow_submit - a human has looked at it by then."""
    job, factory, created = patched(tier="dream_review")
    result = base.apply_to_job(job, factory, {}, Path("r.pdf"), mode="REVIEW", allow_submit=True)
    assert created["adapter"].submitted is True
    assert result.status == "submitted"


def test_submit_is_blocked_when_the_presubmit_check_cannot_run(patched, monkeypatch):
    """The gate must fail CLOSED. An earlier version returned [] on error, which the caller
    read as "clear to submit" - a safety gate that silently disables itself."""
    job, factory, created = patched()

    def boom(self):
        raise RuntimeError("page gone")

    monkeypatch.setattr(base.BaseApplyAdapter, "empty_required_fields",
                        lambda self: [base.PRESUBMIT_CHECK_FAILED])
    result = base.apply_to_job(job, factory, {}, Path("r.pdf"), mode="AUTO")
    assert created["adapter"].submitted is False
    assert result.status == "needs_human"


def test_submit_is_blocked_when_a_required_field_is_empty_in_the_dom(patched, monkeypatch):
    """Even when the adapter's own bookkeeping says everything is answered."""
    job, factory, created = patched()
    monkeypatch.setattr(base.BaseApplyAdapter, "empty_required_fields",
                        lambda self: ["Are you willing to travel?"])
    result = base.apply_to_job(job, factory, {}, Path("r.pdf"), mode="AUTO")
    assert created["adapter"].submitted is False
    assert result.status == "needs_human"
    assert any("travel" in u for u in result.unanswered)
