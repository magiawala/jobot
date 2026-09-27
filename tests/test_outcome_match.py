"""Matching a recruiter email to the application it concerns.

A wrong match is worse than no match: it credits the wrong resume variant and score band,
quietly corrupting the metrics the tracking exists to produce. These tests pin the cases where
refusing to match is the correct answer.
"""
from __future__ import annotations

from jobbot.outcomes.match import match_email

APPS = [
    {"id": 1, "company": "Figma", "title": "Product Designer, CMS"},
    {"id": 2, "company": "Ramp", "title": "Design Engineer"},
    {"id": 3, "company": "Notion", "title": "Workflow Designer"},
    {"id": 4, "company": "Warner Bros Discovery", "title": "Senior Designer"},
]


def test_matches_on_sender_domain():
    m = match_email("careers@figma.com", "Your application", "Hello", APPS)
    assert m and m.application_id == 1 and m.how == "domain"


def test_matches_on_company_name_in_body():
    m = match_email("no-reply@greenhouse.io", "Update", "Thanks for applying to Ramp.", APPS)
    assert m and m.application_id == 2


def test_title_agreement_raises_confidence():
    m = match_email("no-reply@greenhouse.io", "Your application to Figma",
                    "Regarding the Product Designer CMS role", APPS)
    assert m and m.application_id == 1 and m.confidence == "high"


def test_ats_relay_domain_does_not_identify_the_company():
    """greenhouse.io sends on behalf of hundreds of companies - the domain says nothing."""
    m = match_email("no-reply@greenhouse.io", "An update", "We are moving forward.", APPS)
    assert m is None


def test_company_name_must_be_a_whole_word():
    """Substring matching would let "Ramp" match "rampart" and "Notion" match "notional"."""
    m = match_email("hi@example.com", "Rampart Security", "A rampart of notional value.", APPS)
    assert m is None


def test_refuses_when_two_companies_both_match():
    m = match_email("no-reply@greenhouse.io", "Figma and Ramp",
                    "Comparing offers from Figma and Ramp.", APPS)
    assert m is None


def test_multiword_company_matches_when_all_tokens_present():
    m = match_email("no-reply@greenhouse.io", "Update",
                    "Warner Bros Discovery has reviewed your application.", APPS)
    assert m and m.application_id == 4


def test_unrelated_mail_matches_nothing():
    m = match_email("newsletter@somewhere.com", "Weekly design links",
                    "Here are this week's articles.", APPS)
    assert m is None
