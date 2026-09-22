import pytest

from jobbot.resume.validate import FactBoundary, build_fact_boundary, validate_resume

MASTER = {
    "experience": [
        {"company": "Acme Inc.", "title": "Product Designer", "start_date": "Jan 2023", "end_date": "Present",
         "location": "Remote", "bullets": ["Shipped a redesign that lifted conversion by 12%"]},
    ],
    "education": [{"school": "State University", "degree": "BS Computer Science"}],
    "skills": {"Design": ["Figma", "Sketch"]},
}
LIBRARY = {
    "bullets": {"acme": [{"text": "Shipped a redesign that lifted conversion by 12%", "sources": ["a.pdf"]},
                         {"text": "Ran 15 usability tests with 20+ participants", "sources": ["b.pdf"]}]},
    "skills": {"Design": ["Figma", "Sketch"], "Tools": ["Maze"]},
    "education_by_source": {"a": [{"school": "State University", "degree": "BS Computer Science"}]},
}


@pytest.fixture
def boundary() -> FactBoundary:
    return build_fact_boundary(MASTER, LIBRARY)


def make_candidate(**overrides):
    base = {
        "experience": [{"company": "Acme Inc.", "title": "Product Designer", "start_date": "Jan 2023",
                       "end_date": "Present", "location": "Remote",
                       "bullets": ["Shipped a redesign that lifted conversion by 12%"]}],
        "education": [{"school": "State University", "degree": "BS Computer Science"}],
        "skills": {"Design": ["Figma"]},
        "contact": {}, "additional_info": [],
    }
    base.update(overrides)
    return base


def test_valid_candidate_passes(boundary):
    result = validate_resume(make_candidate(), boundary)
    assert result.ok, result.errors


def test_invented_skill_is_rejected(boundary):
    """The model invents a skill (e.g. 'Blender') the person never listed - must be caught."""
    candidate = make_candidate(skills={"Design": ["Figma", "Blender"]})
    result = validate_resume(candidate, boundary)
    assert not result.ok
    assert any("Blender" in e for e in result.errors)


def test_invented_employer_is_rejected(boundary):
    candidate = make_candidate(experience=[{"company": "Shadow Corp", "title": "Designer",
                                            "start_date": "Jan 2023", "end_date": "Present", "bullets": []}])
    result = validate_resume(candidate, boundary)
    assert not result.ok
    assert any("employer" in e for e in result.errors)


def test_fabricated_metric_is_rejected(boundary):
    candidate = make_candidate(experience=[{"company": "Acme Inc.", "title": "Product Designer",
                                            "start_date": "Jan 2023", "end_date": "Present", "location": "Remote",
                                            "bullets": ["Shipped a redesign that lifted conversion by 97%"]}])
    result = validate_resume(candidate, boundary)
    assert not result.ok
    assert any("97%" in e for e in result.errors)


def test_bullet_using_a_different_real_bullets_numbers_passes(boundary):
    """Pulling in a different REAL bullet from the wider library (not on the master) is allowed -
    its numbers (15, 20+) are real, just not on the specific bullet that made the master."""
    candidate = make_candidate(experience=[{"company": "Acme Inc.", "title": "Product Designer",
                                            "start_date": "Jan 2023", "end_date": "Present", "location": "Remote",
                                            "bullets": ["Ran 15 usability tests with 20+ participants"]}])
    result = validate_resume(candidate, boundary)
    assert result.ok, result.errors


def test_title_drift_is_rejected(boundary):
    candidate = make_candidate(experience=[{"company": "Acme Inc.", "title": "Senior Product Designer",
                                            "start_date": "Jan 2023", "end_date": "Present", "location": "Remote",
                                            "bullets": []}])
    result = validate_resume(candidate, boundary)
    assert not result.ok
    assert any("title mismatch" in e for e in result.errors)


def test_date_drift_is_rejected(boundary):
    candidate = make_candidate(experience=[{"company": "Acme Inc.", "title": "Product Designer",
                                            "start_date": "Jan 2020", "end_date": "Present", "location": "Remote",
                                            "bullets": []}])
    result = validate_resume(candidate, boundary)
    assert not result.ok
    assert any("date mismatch" in e for e in result.errors)


def test_unknown_school_is_rejected(boundary):
    candidate = make_candidate(education=[{"school": "Fabricated University", "degree": "PhD"}])
    result = validate_resume(candidate, boundary)
    assert not result.ok
    assert any("school" in e for e in result.errors)


def test_truncated_bullet_is_rejected(boundary):
    """Caught in practice: Claude cut a bullet off mid-sentence ('...as a', '...that') while
    'lightly rewording' it. Must be caught even though it introduces no fabricated numbers."""
    candidate = make_candidate(experience=[{"company": "Acme Inc.", "title": "Product Designer",
                                            "start_date": "Jan 2023", "end_date": "Present", "location": "Remote",
                                            "bullets": ["Shipped a redesign that lifted conversion by"]}])
    result = validate_resume(candidate, boundary)
    assert not result.ok
    assert any("truncated" in e for e in result.errors)


def test_complete_bullet_ending_in_percent_is_not_flagged_as_truncated(boundary):
    result = validate_resume(make_candidate(), boundary)  # ends "...conversion by 12%"
    assert result.ok, result.errors


def test_visa_status_is_rejected_for_us_format(boundary):
    candidate = make_candidate(additional_info=["Visa Status: Requires Sponsorship"])
    result = validate_resume(candidate, boundary)
    assert not result.ok
    assert any("visa" in e.lower() for e in result.errors)
