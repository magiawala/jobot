"""Classifies a recruiter email into an application stage.

Rules first, Claude only for what the rules can't settle. Most of this mail is templated, so
patterns handle the large majority for free, and the batched Claude fallback stays cheap.

The asymmetry that shapes the thresholds: calling a rejection an interview invite would make
Devanshu miss a real one, so `interview` and `offer` require an explicit signal rather than the
absence of a rejection phrase.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Stages, ordered by how far through the funnel they are.
STAGES = ("auto_ack", "rejected", "screen", "interview", "offer", "unknown")

# Automated "we got your application" receipts. Recognised so they don't count as a response -
# treating an auto-acknowledgement as a reply would inflate the response rate enormously.
AUTO_ACK = re.compile(
    r"thank you for (your interest|applying|your application)"
    r"|we('ve| have) received your application"
    r"|your application (has been|was) received"
    r"|application (received|submitted|confirmation)"
    r"|this is an automated (message|response|confirmation)"
    r"|do not reply to this (email|message)"
    r"|we are reviewing your application"
    r"|thanks for applying",
    re.I,
)

REJECTED = re.compile(
    r"\b(unfortunately|regret to inform|we('re| are) sorry)\b"
    r"|not (moving|move) forward"
    r"|will not be (moving|proceeding)"
    r"|decided (not to (move|proceed)|to (move forward|proceed) with other)"
    r"|other candidates whose"
    r"|pursue other candidates"
    r"|no longer under consideration"
    r"|not (be )?select(ed|ing) (you )?(to|for)"
    r"|we('ve| have) filled (the|this) (role|position)"
    r"|position has been filled"
    r"|keep your (resume|details|profile) on file",
    re.I,
)

# A recruiter proposing a first conversation.
SCREEN = re.compile(
    r"(recruiter|initial|intro(ductory)?|phone|screening) (call|chat|conversation|screen)"
    r"|\b(30|20|15)[- ]minute (call|chat)"
    r"|would you be (available|open) (for|to)"
    r"|like to (set up|schedule|find) (a )?(time|call|chat)"
    r"|chat (with|to) (you )?about (your|the) (application|background)"
    r"|learn more about your (background|experience)",
    re.I,
)

# A scheduled or offered interview stage.
INTERVIEW = re.compile(
    r"\binterview\b"
    r"|(technical|design|portfolio|onsite|on-site|final) (round|stage|review|presentation)"
    r"|take[- ]home (assignment|exercise|challenge)"
    r"|design (challenge|exercise)"
    r"|next (round|stage) of",
    re.I,
)

OFFER = re.compile(
    r"\b(offer letter|job offer|we('d| would) like to offer|extend(ing)? (you )?an offer)\b"
    r"|pleased to offer"
    r"|welcome to the team",
    re.I,
)

# Mail that is about a job search but not about one of Devanshu's applications.
NOT_A_REPLY = re.compile(
    r"job alert|new jobs? (for|matching)|jobs you may|recommended for you"
    r"|newsletter|unsubscribe from job alerts|weekly digest"
    r"|complete your profile|finish your application",
    re.I,
)


# Senders whose mail is never a reply to a job application: marketing, social and transactional.
# Without this, a LinkedIn Sales Navigator ad classified as an "offer" and an Amazon Locker
# pickup notice as a "screen" - harmless while they matched no application, but a marketing mail
# FROM a company you applied to would match it and record a false outcome. events@figma.com
# announcing a conference result was classified "rejected", and Figma is a live application.
MARKETING_SENDERS = re.compile(
    r"@(em\.|e\.|marketing\.|news\.|email\.|mail\.)?"
    r"(linkedin|amazon|amazonses|jetblue|united|delta|uber|lyft|doordash|instacart|spotify|"
    r"netflix|apple|paypal|venmo|chase|amex|figma|notion|slack|zoom|dropbox|medium|substack)\."
    r"|@.*\b(marketing|newsletter|promo|deals|noreply-social|notifications?)\b",
    re.I,
)

# Mail genuinely about an application says so. Requiring this stops a company's marketing mail
# from being read as a reply just because a stage phrase appears in it.
APPLICATION_CONTEXT = re.compile(
    r"\bapplication\b|\bapplied\b|\bcandidate\b|\brecruit(er|ing|ment)\b|\bhiring\b"
    r"|\bposition\b|\brole\b|\bjob\b|\binterview\b|\bresume\b|\bcv\b",
    re.I,
)

# ATS relays only carry application mail, so context is implied for them.
ATS_SENDERS = re.compile(
    r"@(.*\.)?(greenhouse-mail\.io|greenhouse\.io|ashbyhq\.com|hire\.lever\.co|lever\.co|"
    r"myworkday\.com|workday\.com|smartrecruiters\.com|icims\.com|jobvite\.com)",
    re.I,
)


def looks_like_application_mail(from_addr: str, subject: str, body: str) -> bool:
    """Whether this message is plausibly about a job application at all.

    Checked before any stage is assigned: a stage phrase inside a marketing email is not a
    signal about an application, and if that mail came from a company we applied to it would be
    matched and recorded as a real outcome.
    """
    if ATS_SENDERS.search(from_addr or ""):
        return True
    if MARKETING_SENDERS.search(from_addr or ""):
        return False
    return bool(APPLICATION_CONTEXT.search(f"{subject}\n{body[:2000]}"))


@dataclass
class Classification:
    stage: str
    confidence: str          # high | low
    evidence: str            # the phrase that decided it, for auditing
    needs_claude: bool = False


def _first_match(pattern: re.Pattern[str], text: str) -> str:
    m = pattern.search(text)
    return (m.group(0) or "").strip()[:120] if m else ""


def classify(subject: str, body: str, from_addr: str = "") -> Classification:
    """Classifies one message. `body` should already be plain text.

    `from_addr` is optional only so existing callers keep working; pass it whenever you have it,
    because the sender is what distinguishes a recruiter's mail from a company's marketing.
    """
    text = f"{subject}\n{body}"

    if NOT_A_REPLY.search(text):
        return Classification("unknown", "high", "job alert / newsletter, not a reply")

    if from_addr and not looks_like_application_mail(from_addr, subject, body):
        return Classification("unknown", "high", "not application mail (marketing/transactional)")

    # Order matters and is not the funnel order. A rejection that mentions "interview"
    # ("thank you for interviewing with us... unfortunately") is a rejection, so rejection is
    # tested before the positive stages.
    if (hit := _first_match(OFFER, text)):
        return Classification("offer", "high", hit)
    if (hit := _first_match(REJECTED, text)):
        return Classification("rejected", "high", hit)
    if (hit := _first_match(INTERVIEW, text)):
        return Classification("interview", "high", hit)
    if (hit := _first_match(SCREEN, text)):
        return Classification("screen", "high", hit)

    # Auto-acknowledgements are checked LAST among the positives: a real reply often repeats
    # "thank you for applying" before getting to the point.
    if (hit := _first_match(AUTO_ACK, text)):
        return Classification("auto_ack", "high", hit)

    return Classification("unknown", "low", "", needs_claude=True)


def is_response(stage: str) -> bool:
    """Whether this stage counts as a real human response for funnel metrics.

    An auto-acknowledgement is not a response - counting those would make the response rate
    approximately the submission rate and tell us nothing.
    """
    return stage in ("rejected", "screen", "interview", "offer")


def is_positive(stage: str) -> bool:
    return stage in ("screen", "interview", "offer")
