"""Tests for the self-improving answer layer: derived answers, expanded matching, and the
learned-answers store that turns each needs_human into a one-time cost."""
from __future__ import annotations

import pytest

from jobbot.apply import learned
from jobbot.apply.answers import classify_label, derive_answer
from jobbot.apply.draft import is_draftable

PROFILE = {"location": {"city": "Boston", "state": "MA", "country": "United States"}}


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(learned, "LEARNED_PATH", tmp_path / "learned_answers.yaml")


# ---- matching gaps found from real postings ----

@pytest.mark.parametrize("label,expected", [
    ("Do you have a legal right to work in the United States?*", "work_authorization"),
    ("Are you legally authorized to work in the country for which you are applying?", "work_authorization"),
    ("Will you now or in the future require sponsorship for employment visa status?", "needs_sponsorship"),
    ("Please tell us how you heard about this opportunity.", "how_did_you_hear"),
    ("What is your desired base compensation for this role?", "desired_salary"),
])
def test_real_world_phrasings_now_match(label, expected):
    assert classify_label(label)[0] == expected


def test_how_did_you_hear_no_longer_matches_location():
    """It previously matched `location` at 85%, which would have typed a city into it."""
    assert classify_label("Please tell us how you heard about this opportunity.")[0] != "location"


# ---- derived answers ----

def test_state_list_question_derives_no_when_state_absent():
    label = "Do you live in one of the following states? Alabama, Alaska, Delaware, Hawaii"
    assert derive_answer(label, PROFILE) == "No"


def test_state_list_question_derives_yes_when_state_present():
    label = "Do you live in one of the following states? Massachusetts, New York"
    assert derive_answer(label, PROFILE) == "Yes"


def test_unrelated_question_derives_nothing():
    assert derive_answer("Export Control Question:", PROFILE) is None


# ---- drafting scope ----

def test_curly_apostrophe_label_is_still_draftable():
    """Form labels use curly quotes; ASCII-only patterns silently missed them."""
    assert is_draftable("What’s a project you’re most proud of?") is True


def test_questions_needing_facts_we_lack_are_not_drafted():
    for label in ["What are your salary expectations?",
                  "Do you hold an active security clearance?",
                  "Please provide your visa status details"]:
        assert is_draftable(label) is False, label


# ---- learned answers ----

def test_pending_question_is_recorded_once_and_counts_recurrence():
    assert learned.record_pending("Export Control Question:", ats="greenhouse") is True
    assert learned.record_pending("Export Control Question:", ats="greenhouse") is False
    data = learned.load()
    assert len(data["pending"]) == 1
    assert data["pending"][0]["seen_count"] == 2


def test_answering_a_pending_question_makes_it_reusable():
    learned.record_pending("Export Control Question:", ats="greenhouse")
    data = learned.load()
    data["pending"][0]["answer"] = "No"
    learned.save(data)

    assert learned.promote_answered() == 1
    # fuzzy-matched, so a differently-worded variant of the same question still resolves
    assert learned.find_answer("Export Control Question:") == "No"
    assert learned.find_answer("Export control question") == "No"


def test_unknown_question_has_no_learned_answer():
    assert learned.find_answer("Something never seen before") is None


def test_options_are_captured_so_dropdowns_can_be_answered():
    learned.record_pending("Language Skill(s)", ats="lever", options=["English (ENG)", "Spanish (SPA)"])
    entry = learned.load()["pending"][0]
    assert entry["options"] == ["English (ENG)", "Spanish (SPA)"]


# ---- EEO matching precision ----

@pytest.mark.parametrize("label,expected", [
    # the false positive: "ability" scored 82% against "disability" via partial_ratio and wrote
    # the EEO decline answer into a free-text box on a real application
    ("Please add up to three bullets showing exceptional ability", None),
    ("What is your design process?", None),
    ("Disability Status", "disability_status"),
    ("Do you have a disability?", "disability_status"),
    ("Voluntary Self-Identification of Disability", "disability_status"),
    ("Veteran Status", "veteran_status"),
    ("Are you Hispanic/Latino?", "hispanic_latino"),
])
def test_eeo_matching_requires_a_word_boundary(label, expected):
    assert classify_label(label)[0] == expected


# ---- option-label detection ----

@pytest.mark.parametrize("text", [
    # Greenhouse records each checkbox choice as its own queue entry, so these arrived as
    # "questions". Listing them by name doesn't generalise - every company has its own.
    "Monkey MindPong", "Neuralink Show & Tell", "Instagram", "Word of mouth",
    "Co-Star jobs page", "Women in Tech", "Projects", "Affirmation",
])
def test_option_labels_are_not_treated_as_questions(text):
    from jobbot.apply.autolearn import looks_like_option_label
    assert looks_like_option_label(text) is True


@pytest.mark.parametrize("text", [
    "What is the country of your birth?",
    "Have you worked at a startup?",
    "Do you require a visa?",
    "Please list all countries of which you are a citizen",
    "If you were referred to Livefront, tell us how.",
    "Which onsite location would you like to apply to?",
])
def test_real_questions_are_kept(text):
    from jobbot.apply.autolearn import looks_like_option_label
    assert looks_like_option_label(text) is False
