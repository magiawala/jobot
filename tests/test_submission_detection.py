"""Which POSTs count as an actual application submission.

This has been wrong in both directions against live traffic, which is why it is pinned down
here: once too broad (a page-load GraphQL op reported as a confirmed submission for an
application that never went out) and once too narrow (missing Ashby's real submission op).
"""
from __future__ import annotations

import pytest

from jobbot.apply.base import is_submission_endpoint

REAL_SUBMISSIONS = [
    # verified: this one produced a confirmation email
    "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiSubmitSingleApplicationFormAction",
    "https://api.greenhouse.io/v1/applications",
    "https://jobs.lever.co/x/apply/submitApplication",
    "https://example.com/api/createApplication",
]

NOT_SUBMISSIONS = [
    # the false positive: routine page-load query, fires before anything is filled
    "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiOrganizationFromHostedJobsPageName",
    "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPostingFromHostedJobsPage",
    "https://www.recaptcha.net/recaptcha/api2/reload?k=abc",
    "https://www.recaptcha.net/recaptcha/api2/clr?k=abc",
    "https://d7ea7d1e203e986f74f02cde32e9be83.seondnsresolve.com/",
    "https://boards.greenhouse.io/embed/job_app?for=x",
    "https://example.com/analytics/track",
]


@pytest.mark.parametrize("url", REAL_SUBMISSIONS)
def test_real_submission_endpoints_are_recognised(url):
    assert is_submission_endpoint(url) is True


@pytest.mark.parametrize("url", NOT_SUBMISSIONS)
def test_routine_traffic_is_not_mistaken_for_a_submission(url):
    assert is_submission_endpoint(url) is False
