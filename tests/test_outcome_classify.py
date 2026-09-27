"""Classifying recruiter email into a funnel stage.

The failure modes are asymmetric, which is what the ordering in classify() is for:
  - calling a rejection an interview would make Devanshu miss a real invitation
  - counting auto-acknowledgements as responses would make the response rate meaningless
"""
from __future__ import annotations

import pytest

from jobbot.outcomes.classify import classify, is_positive, is_response

REJECTIONS = [
    ("Update on your application", "Unfortunately we have decided not to move forward with your application."),
    ("Your application to Acme", "We regret to inform you that we are pursuing other candidates."),
    ("Application status", "We've decided to move forward with other candidates whose experience more closely matches."),
    ("Re: Product Designer", "The position has been filled. We'll keep your resume on file."),
    # mentions "interview" but is still a rejection - this is why order matters
    ("Thanks for interviewing", "Thank you for interviewing with us. Unfortunately we won't be moving forward."),
]

SCREENS = [
    ("Quick chat?", "Would you be available for a 30-minute call next week to discuss your application?"),
    ("Your application", "I'd like to set up a time for an intro call to learn more about your background."),
]

INTERVIEWS = [
    ("Next steps", "We'd like to invite you to a technical interview with the design team."),
    ("Design exercise", "The next round is a take-home assignment followed by a portfolio review."),
]

OFFERS = [
    ("Great news", "We would like to offer you the Product Designer position."),
    ("Offer letter attached", "Please find your offer letter attached. Welcome to the team!"),
]

AUTO_ACKS = [
    ("Application received", "Thank you for applying to Acme. We have received your application."),
    ("Thanks!", "This is an automated message confirming your application was received. Do not reply to this email."),
]

NOT_REPLIES = [
    ("5 new jobs for you", "New jobs matching your search: Product Designer at ..."),
    ("Your weekly digest", "Jobs you may be interested in this week. Unsubscribe from job alerts."),
]


@pytest.mark.parametrize("subject,body", REJECTIONS)
def test_rejections(subject, body):
    assert classify(subject, body).stage == "rejected"


@pytest.mark.parametrize("subject,body", SCREENS)
def test_recruiter_screens(subject, body):
    assert classify(subject, body).stage == "screen"


@pytest.mark.parametrize("subject,body", INTERVIEWS)
def test_interviews(subject, body):
    assert classify(subject, body).stage == "interview"


@pytest.mark.parametrize("subject,body", OFFERS)
def test_offers(subject, body):
    assert classify(subject, body).stage == "offer"


@pytest.mark.parametrize("subject,body", AUTO_ACKS)
def test_auto_acknowledgements_are_not_responses(subject, body):
    c = classify(subject, body)
    assert c.stage == "auto_ack"
    assert is_response(c.stage) is False


@pytest.mark.parametrize("subject,body", NOT_REPLIES)
def test_job_alerts_are_not_replies(subject, body):
    assert classify(subject, body).stage == "unknown"


def test_unrecognised_mail_is_escalated_rather_than_guessed():
    c = classify("Hello", "Following up on our conversation from last week about the role.")
    assert c.needs_claude is True


def test_response_and_positive_definitions():
    assert [is_response(s) for s in ("rejected", "screen", "interview", "offer")] == [True] * 4
    assert is_response("auto_ack") is False
    assert [is_positive(s) for s in ("screen", "interview", "offer")] == [True] * 3
    assert is_positive("rejected") is False


# ---- marketing mail must never be read as a reply ----

REAL_MARKETING = [
    # every one of these was classified as a funnel stage on the first live run
    ("linkedin@em.linkedin.com", "Try Sales Navigator at 50% off for 2 months", "Upgrade today."),
    ("amazonlockers@amazon.com", "You have a package to pick up", "Pick up at the Amazon Locker."),
    ("jetblueairways@marketing.jetblue.com", "Where would you go with 70,000 bonus points?", "Book now."),
    ("groups-noreply@linkedin.com", "Don't miss conversations in React Js", "Join the discussion."),
    # the dangerous one: a company we have a live application with, sending marketing
    ("events@figma.com", "Config 2026 India Application Notification",
     "Unfortunately your application to attend was not selected."),
]


@pytest.mark.parametrize("sender,subject,body", REAL_MARKETING)
def test_marketing_mail_is_not_a_funnel_stage(sender, subject, body):
    assert classify(subject, body, sender).stage == "unknown"


REAL_ATS_REPLIES = [
    ("no-reply@ashbyhq.com", "Thank you for your application to Rogo",
     "You were not selected for this role.", "rejected"),
    ("no-reply@ashbyhq.com", "Thanks for applying to Cardless!",
     "Thanks for applying. We'll be in touch.", "auto_ack"),
    ("no-reply@us.greenhouse-mail.io", "Important information about your application",
     "Unfortunately we are moving forward with other candidates.", "rejected"),
]


@pytest.mark.parametrize("sender,subject,body,expected", REAL_ATS_REPLIES)
def test_genuine_ats_replies_still_classify(sender, subject, body, expected):
    assert classify(subject, body, sender).stage == expected


def test_a_direct_recruiter_email_still_classifies():
    """Not from an ATS, but unmistakably about an application."""
    c = classify("Your application", "I'd like to schedule an intro call about your application.",
                 "sarah@somestartup.com")
    assert c.stage == "screen"
