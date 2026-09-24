"""Which free-text questions may be answered by Claude from the resume.

Started as a list of exact phrasings and kept missing new ones - every posting words these
differently - so it is decided by signal now. These tests pin both directions, because the
failure modes are asymmetric: missing a draftable question costs an application, while drafting
one that needs a fact we don't hold puts a false statement on a real application.
"""
from __future__ import annotations

import pytest

from jobbot.apply.draft import is_draftable

DRAFTABLE = [
    "Based on what you know about Livefront right now, why do you want to be a part of our team?",
    "Briefly describe a specific instance where you leveraged an AI-assisted tool to accelerate design",
    "What is it about Gamma or this specific role that made you apply?",
    "How do you take a complex or messy workflow and make it feel simple? Show me an example.",
    "Pitch me one bold idea for how a scared parent should first experience Alpaca online",
    "What do you think qualifies you for the design technologist role?",
    "Why Levelpath?",
    "Please add up to three bullets showing exceptional ability",
    "What about this role at Infisical interests you, and what type of work do you hope to focus on?",
    "What’s a project you’re most proud of?",
]

# Each needs a fact about Devanshu that appears nowhere in the profile or resume.
NOT_DRAFTABLE = [
    "What was your undergraduate GPA?",
    "What is the country of your birth?",
    "Please list all countries of which you are a citizen",
    "What are your salary expectations?",
    "Do you hold a security clearance?",
    "Have you ever been fired or asked to resign to avoid being fired from a job?",
    "Can you perform the essential functions of this job with or without reasonable accomodation?",
    "Do you now or will you ever require an employer-sponsored work visa?",
    # closed-form: a dropdown or single value, not an essay
    "Are you willing to move to SF/Vancouver and work in person with us?",
    "How many years of professional experience do you have?",
    "What is your current geographic availability for this role?",
    "What is your go-to programming language?",
]


@pytest.mark.parametrize("label", DRAFTABLE)
def test_open_ended_questions_are_draftable(label):
    assert is_draftable(label) is True


@pytest.mark.parametrize("label", NOT_DRAFTABLE)
def test_questions_needing_unknown_facts_are_never_drafted(label):
    assert is_draftable(label) is False
