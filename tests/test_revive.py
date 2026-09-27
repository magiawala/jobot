"""Auto-revival of stale needs_human applications.

Stale records don't self-heal: each time a class of question becomes answerable, applications
already parked in needs_human stay parked. The highest-scoring job in the database sat blocked
for days on a work-authorization question that had been answerable the whole time.
"""
from __future__ import annotations

import json

import pytest

from jobbot import run


@pytest.fixture
def stub_answerable(monkeypatch):
    """Control which questions count as answerable."""
    def configure(answerable: set[str]):
        monkeypatch.setattr("jobbot.apply.answers.classify_label",
                            lambda q: (("key" if q in answerable else None), False, 100))
        monkeypatch.setattr("jobbot.apply.answers.derive_answer", lambda q, p: None)
        monkeypatch.setattr("jobbot.apply.draft.is_draftable", lambda q: False)
        monkeypatch.setattr("jobbot.apply.learned.find_answer", lambda q: None)
    return configure


def _rows(monkeypatch, rows, updated):
    class Result(list):
        def fetchall(self):
            return list(self)

    class FakeConn:
        def execute(self, sql, params=()):
            if sql.strip().upper().startswith("SELECT"):
                return Result(rows)
            return None
        def commit(self):
            pass

    class Session:
        def __enter__(self):
            return FakeConn()
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(run.db, "session", lambda: Session())
    monkeypatch.setattr(run.db, "update_application",
                        lambda conn, app_id, **kw: updated.append((app_id, kw.get("status"))))
    monkeypatch.setattr(run.config, "profile", lambda: {})


def test_revives_when_every_blocker_is_now_answerable(monkeypatch, stub_answerable):
    stub_answerable({"Are you authorized to work in the US?"})
    updated = []
    _rows(monkeypatch, [{
        "id": 1, "company": "Figma", "title": "Product Designer", "attempts": 1,
        "last_error": "unanswered required",
        "filled_answers": json.dumps({"unanswered": ["REQUIRED: Are you authorized to work in the US?"]}),
    }], updated)
    assert run.revive_stale() == 1
    assert updated == [(1, "queued")]


def test_does_not_revive_when_any_blocker_remains(monkeypatch, stub_answerable):
    """A partial fix would just burn the application slot and park it again."""
    stub_answerable({"Answerable one"})
    updated = []
    _rows(monkeypatch, [{
        "id": 2, "company": "Acme", "title": "Designer", "attempts": 1,
        "last_error": "unanswered required",
        "filled_answers": json.dumps({"unanswered": [
            "REQUIRED: Answerable one", "REQUIRED: What is the country of your birth?"]}),
    }], updated)
    assert run.revive_stale() == 0
    assert updated == []


@pytest.mark.parametrize("error", [
    "interactive CAPTCHA challenge present",
    "login/account wall detected",
    "form too long: 30 fields",
    "Workday needs an account per company - apply by hand",
    "no fields were filled - form likely did not render",
])
def test_never_revives_blockers_a_person_must_clear(monkeypatch, stub_answerable, error):
    stub_answerable({"anything"})
    updated = []
    _rows(monkeypatch, [{
        "id": 3, "company": "Acme", "title": "Designer", "attempts": 1, "last_error": error,
        "filled_answers": json.dumps({"unanswered": ["REQUIRED: anything"]}),
    }], updated)
    assert run.revive_stale() == 0


def test_revives_a_fill_failure_field_without_needing_an_answer(monkeypatch, stub_answerable):
    """23 applications sat parked on "Resume" - a field the adapter failed to upload, not a
    question. There is no answer to learn, so a re-run is the right response."""
    stub_answerable(set())
    updated = []
    _rows(monkeypatch, [{
        "id": 4, "company": "Clipboard", "title": "Design Engineer", "attempts": 1,
        "last_error": "unanswered required",
        "filled_answers": json.dumps({"unanswered": ["REQUIRED: Resume"]}),
    }], updated)
    assert run.revive_stale() == 1


def test_stops_reviving_after_repeated_attempts(monkeypatch, stub_answerable):
    """A form that genuinely cannot be filled must not loop forever."""
    stub_answerable(set())
    updated = []
    _rows(monkeypatch, [{
        "id": 5, "company": "Acme", "title": "Designer", "attempts": run.MAX_REVIVE_ATTEMPTS,
        "last_error": "unanswered required",
        "filled_answers": json.dumps({"unanswered": ["REQUIRED: Resume"]}),
    }], updated)
    assert run.revive_stale() == 0
