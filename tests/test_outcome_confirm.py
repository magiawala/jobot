"""A reply from the company proves the application landed.

This matters because detecting submission from the browser side got it wrong three separate
ways: reading page text, a disabled submit button read as success, and a page-load GraphQL POST
counted as a submission. An email from the company is evidence that doesn't depend on any of
that, so any matched reply - including a plain auto-acknowledgement - confirms the submission.
"""
from __future__ import annotations

import json

import pytest

from jobbot import outcomes
from jobbot.outcomes.classify import classify
from jobbot.outcomes.match import match_email


def test_auto_acknowledgement_is_still_proof_of_submission():
    """It is NOT a response for funnel purposes, but it does prove the form went through."""
    c = classify("Thanks for applying to Cardless!", "Thanks for applying. We'll be in touch.",
                 "no-reply@ashbyhq.com")
    assert c.stage == "auto_ack"
    from jobbot.outcomes.classify import is_response
    assert is_response(c.stage) is False          # not a response
    hit = match_email("no-reply@ashbyhq.com", "Thanks for applying to Cardless!",
                      "Thanks for applying.", [{"id": 7, "company": "Cardless", "title": "Product Designer"}])
    assert hit is not None                         # but it does identify the application


def test_stage_rank_never_walks_an_application_backwards():
    """A later auto-acknowledgement must not undo a recorded interview."""
    rank = outcomes.STAGE_RANK
    assert rank["auto_ack"] < rank["rejected"] < rank["screen"] < rank["interview"] < rank["offer"]
