import pytest

from jobbot.apply.answers import (
    build_answer_book, classify_label, is_required, normalize_label, pick_option,
)

PROFILE = {
    "name": {"full": "Devanshu Magiawala", "first": "Devanshu", "last": "Magiawala"},
    "email": "devanshumag@gmail.com",
    "phone": "(317) 345-3293",
    "location": {"city": "Boston", "state": "MA"},
    "links": {"linkedin": "linkedin.com/in/magiawala", "portfolio": "magiawala.github.io"},
    "work_authorization": {"authorized_in_us": None, "needs_sponsorship_now": None,
                           "needs_sponsorship_future": None},
    "preferences": {"willing_to_relocate": True, "desired_salary_target": 160000,
                    "earliest_start_date": "2 weeks"},
    "standard_answers": {"how_did_you_hear": "Company website", "over_18": "Yes",
                         "previously_worked_here": "No", "school": "Indiana University Indianapolis"},
    "eeo": {"gender": "decline", "veteran_status": "decline", "disability_status": "decline"},
}


def test_unset_work_authorization_is_blank_not_no():
    """A null work-authorization must never become "No" - that's a misrepresentation on a real
    application. It has to stay blank so the job routes to needs_human."""
    book = build_answer_book(PROFILE)
    assert book.get("work_authorization") == ""
    assert book.get("needs_sponsorship") == ""


def test_set_work_authorization_answers_normally():
    profile = {**PROFILE, "work_authorization": {"authorized_in_us": True,
                                                 "needs_sponsorship_now": False,
                                                 "needs_sponsorship_future": False}}
    book = build_answer_book(profile)
    assert book.get("work_authorization") == "Yes"
    assert book.get("needs_sponsorship") == "No"


def test_wrong_but_similar_school_is_rejected_under_strict():
    """Caught live: 'Indiana University Indianapolis' matched a dropdown's 'University of
    Indianapolis' - a different school entirely."""
    options = ["University of Indianapolis", "Purdue University", "Other"]
    assert pick_option(options, "Indiana University Indianapolis", strict=True) is None


def test_exact_school_still_matches_under_strict():
    options = ["Indiana University Indianapolis", "Other"]
    assert pick_option(options, "Indiana University Indianapolis", strict=True) == \
        "Indiana University Indianapolis"


def test_pronunciation_field_does_not_match_name():
    """Caught live: 'Name Pronunciation | How do you pronounce your name?' got the legal name."""
    key, _eeo, _score = classify_label("Name Pronunciation | How do you pronounce your name?")
    assert key is None


def test_plain_name_labels_still_match():
    assert classify_label("Full name")[0] == "full_name"
    assert classify_label("First Name*")[0] == "first_name"


def test_eeo_labels_route_to_eeo():
    key, eeo, _ = classify_label("Veteran Status")
    assert eeo is True and key == "veteran_status"


def test_eeo_picks_a_decline_option():
    options = ["I identify as a protected veteran", "I am not a protected veteran",
               "I decline to self-identify for protected veteran status"]
    assert pick_option(options, "decline", eeo=True) == \
        "I decline to self-identify for protected veteran status"


def test_yes_no_options_match_exactly():
    assert pick_option(["Yes", "No"], "No") == "No"
    assert pick_option(["Yes", "No"], "Yes") == "Yes"


def test_unrelated_option_set_returns_none_rather_than_guessing():
    assert pick_option(["Banana", "Cucumber"], "Yes") is None


@pytest.mark.parametrize("label,expected", [
    ("First Name*", True),
    ("Full name\n✱", True),
    ("LinkedIn Profile", False),
])
def test_required_detection_handles_asterisk_and_glyph(label, expected):
    assert is_required(label) is expected


def test_normalize_label_strips_markers():
    assert normalize_label("Are you authorized to work? *") == "are you authorized to work"
