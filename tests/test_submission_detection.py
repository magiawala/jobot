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


# ---- apply-URL normalisation ----

@pytest.mark.parametrize("raw,expected", [
    # a live Robinhood posting: `t=gh_src=` is an empty value whose content is itself a
    # parameter name, and Chromium aborted the navigation outright (net::ERR_ABORTED)
    ("https://boards.greenhouse.io/robinhood/jobs/8240638?t=gh_src=&gh_jid=8240638",
     "https://boards.greenhouse.io/robinhood/jobs/8240638?gh_jid=8240638"),
    # gh_jid is kept - some boards need it to select which posting to show
    ("https://careers.toasttab.com/jobs?gh_jid=7989176",
     "https://careers.toasttab.com/jobs?gh_jid=7989176"),
    ("https://x.com/a?utm_source=linkedin&gh_jid=9", "https://x.com/a?gh_jid=9"),
    ("https://x.com/a", "https://x.com/a"),
    ("https://x.com/a?t=", "https://x.com/a"),
])
def test_apply_url_normalisation(raw, expected):
    from jobbot.apply.base import normalize_apply_url
    assert normalize_apply_url(raw) == expected
