import sqlite3

import pytest

from jobbot import score

CFG = {
    "roles": {
        "product_design": {"titles_exact": ["Product Designer", "Senior Product Designer"], "titles_related": ["Interaction Designer"]},
        "design_engineer": {"titles_exact": ["Design Engineer"], "titles_related": ["UI Engineer"]},
        "ux_design": {"titles_exact": ["UX Designer"], "titles_related": ["UI Designer"]},
    },
    "title_blocklist": ["Intern", "Graphic Designer", "Production Designer", "PCB", "FPGA", "Design Manager"],
    "seniority": {
        "target": "mid",
        "soft_title_words": ["Senior"],
        "blocked_title_words": ["Staff", "Principal", "Lead", "Director", "Manager", "Intern", "Junior"],
        "years_required": {"min_ok": 2, "max_ok": 6},
    },
    "salary": {"currency": "USD", "target_min": 130000, "target_max": 200000, "hourly_to_annual_hours": 2080,
              "if_unlisted": "allow_with_penalty", "unlisted_penalty": 10},
    "locations": {"country": "US", "remote_us_ok": True, "cities": []},
    "thresholds": {"skip_below": 50, "tailor_at_or_above": 75},
}


def make_job(**kw):
    defaults = dict(
        id=1, title="Product Designer", company="Acme", location="San Francisco, CA", location_all="San Francisco, CA",
        country_hint="", remote=0, description_text="Figma prototyping design systems", salary_min=150000,
        salary_max=180000, salary_currency="USD", salary_source="structured", salary_interval="year",
    )
    defaults.update(kw)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ",".join(defaults.keys())
    placeholders = ",".join("?" * len(defaults))
    conn.execute(f"CREATE TABLE t ({','.join(f'{k} TEXT' if k not in ('id','remote','salary_min','salary_max') else f'{k} REAL' for k in defaults)})")
    conn.execute(f"INSERT INTO t ({cols}) VALUES ({placeholders})", list(defaults.values()))
    return conn.execute("SELECT * FROM t").fetchone()


def test_mid_level_product_designer_passes_and_tailors():
    job = make_job()
    kb = score.build_keyword_bank() or {"figma", "prototyping", "design systems"}
    pattern = score.compile_keyword_pattern(kb)
    s = score.score_job(job, CFG, pattern, {}, {})
    assert s["tier"] in ("tailor", "variant", "dream_review")
    assert s["role_bucket"] == "product_design"
    assert s["total"] > 0


def test_senior_gets_fewer_seniority_points_but_still_passes():
    job = make_job(title="Senior Product Designer")
    pattern = score.compile_keyword_pattern({"figma"})
    s = score.score_job(job, CFG, pattern, {}, {})
    assert s["seniority_pts"] == 8
    assert s["tier"] != "skip"


def test_staff_title_is_skipped():
    job = make_job(title="Staff Product Designer")
    pattern = score.compile_keyword_pattern({"figma"})
    s = score.score_job(job, CFG, pattern, {}, {})
    assert s["tier"] == "skip"


def test_blocklisted_title_is_skipped():
    job = make_job(title="Production Designer")
    pattern = score.compile_keyword_pattern({"figma"})
    s = score.score_job(job, CFG, pattern, {}, {})
    assert s["tier"] == "skip"


def test_non_us_location_is_skipped():
    job = make_job(location="London, UK", location_all="London, UK", country_hint="GB")
    pattern = score.compile_keyword_pattern({"figma"})
    s = score.score_job(job, CFG, pattern, {}, {})
    assert s["tier"] == "skip"


def test_remote_us_passes():
    job = make_job(location="Remote", location_all="Remote - US", remote=1, salary_min=None, salary_max=None,
                   salary_currency=None, salary_source=None, salary_interval=None)
    pattern = score.compile_keyword_pattern({"figma"})
    s = score.score_job(job, CFG, pattern, {}, {})
    assert s["tier"] != "skip"


def test_salary_too_high_implies_senior_and_is_skipped():
    job = make_job(salary_min=250000, salary_max=300000)
    pattern = score.compile_keyword_pattern({"figma"})
    s = score.score_job(job, CFG, pattern, {}, {})
    assert s["tier"] == "skip"


def test_salary_unlisted_applies_penalty_not_skip():
    job = make_job(salary_min=None, salary_max=None, salary_currency=None, salary_source=None, salary_interval=None)
    pattern = score.compile_keyword_pattern({"figma"})
    s = score.score_job(job, CFG, pattern, {}, {})
    assert s["tier"] != "skip"
    assert s["penalty"] == 10


def test_years_required_out_of_range_is_skipped():
    job = make_job(description_text="We need 8+ years of experience with Figma")
    pattern = score.compile_keyword_pattern({"figma"})
    s = score.score_job(job, CFG, pattern, {}, {})
    assert s["tier"] == "skip"


def test_keywords_are_matched_even_when_skipped():
    """jd_keywords must be filled for EVERY analyzed job, including skipped ones (spec requirement)."""
    job = make_job(title="Staff Product Designer", description_text="Figma and prototyping required")
    pattern = score.compile_keyword_pattern({"figma", "prototyping"})
    s = score.score_job(job, CFG, pattern, {}, {})
    assert s["tier"] == "skip"
    assert set(s["_matched_keywords"]) == {"figma", "prototyping"}


def test_dream_company_always_held_for_review():
    job = make_job()
    pattern = score.compile_keyword_pattern({"figma"})
    companies = {"acme": {"dream": True}}
    s = score.score_job(job, CFG, pattern, {}, companies)
    assert s["tier"] == "dream_review"


def test_blocklisted_company_is_skipped():
    job = make_job(company="BadCo")
    pattern = score.compile_keyword_pattern({"figma"})
    with_bl = score.blocklisted
    try:
        score.blocklisted = lambda name: name.lower() == "badco"
        s = score.score_job(job, CFG, pattern, {}, {})
        assert s["tier"] == "skip"
    finally:
        score.blocklisted = with_bl


def test_fuzzy_title_hardware_design_engineer_blocked():
    job = make_job(title="PCB Design Engineer")
    pattern = score.compile_keyword_pattern({"figma"})
    s = score.score_job(job, CFG, pattern, {}, {})
    assert s["tier"] == "skip"


@pytest.mark.parametrize("text,expected", [
    ("$130,000 - $180,000", (130000.0, 180000.0)),
    ("$130K-$180K", (130000.0, 180000.0)),
    ("130k to 180k", (130000.0, 180000.0)),
])
def test_parse_salary_from_text_formats(text, expected):
    result = score.parse_salary_from_text(text, hourly_to_annual=2080)
    assert result is not None
    lo, hi, _ = result
    assert (lo, hi) == expected


def test_parse_salary_hourly_converts_to_annual():
    result = score.parse_salary_from_text("$60 - $75 per hour", hourly_to_annual=2080)
    assert result is not None
    lo, hi, src = result
    assert lo == 60 * 2080
    assert hi == 75 * 2080
    assert src == "text-hourly"
