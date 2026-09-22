from jobbot.resume.library import (
    ConflictField,
    RoleConflict,
    compare_experience_across_sources,
    extract_bullets_from_text,
    normalize_company,
    render_conflicts_md,
)


def test_normalize_company_strips_parentheticals_and_suffixes():
    assert normalize_company("Integrate BPD (acquired by My Healing Work)") == "integrate bpd"
    assert normalize_company("Acme Corp") == "acme"
    assert normalize_company("Acme, Inc.") == "acme"


def test_extract_bullets_from_text_only_keeps_bulleted_lines():
    text = "Company X\nTitle Y\n• Did a thing that mattered a lot\nNot a bullet line\n- Did another thing here too"
    bullets = extract_bullets_from_text(text)
    assert bullets == ["Did a thing that mattered a lot", "Did another thing here too"]


def test_compare_experience_detects_date_and_title_conflicts():
    structured = {
        "a": {"experience": [{"company": "HP Inc.", "start_date": "Oct 2024", "end_date": "Dec 2024",
                              "title": "Product Researcher", "location": "Indianapolis"}]},
        "b": {"experience": [{"company": "HP Inc.", "start_date": "Oct 2024", "end_date": "May 2025",
                              "title": "Product Designer", "location": "Remote"}]},
    }
    conflicts = compare_experience_across_sources(structured)
    assert len(conflicts) == 1
    fields = {cf.field for cf in conflicts[0].conflicts}
    assert fields == {"end_date", "title", "location"}
    assert "start_date" not in fields  # this one genuinely matches, must not be flagged


def test_compare_experience_no_conflict_when_single_source():
    structured = {"a": {"experience": [{"company": "Solo Co", "start_date": "Jan 2020", "end_date": "Jan 2021",
                                        "title": "Engineer", "location": "Remote"}]}}
    assert compare_experience_across_sources(structured) == []


def test_compare_experience_no_conflict_when_values_agree():
    structured = {
        "a": {"experience": [{"company": "Agree Co", "start_date": "Jan 2020", "end_date": "Jan 2021",
                              "title": "Engineer", "location": "Remote"}]},
        "b": {"experience": [{"company": "Agree Co", "start_date": "Jan 2020", "end_date": "Jan 2021",
                              "title": "Engineer", "location": "Remote"}]},
    }
    assert compare_experience_across_sources(structured) == []


def test_render_conflicts_md_includes_all_fields_and_sources():
    rc = RoleConflict(company="HP Inc.", conflicts=[
        ConflictField(field="end_date", values={"a": "Dec 2024", "b": "May 2025"}),
    ])
    md = render_conflicts_md([rc], ["a note"])
    assert "HP Inc." in md
    assert "Dec 2024" in md
    assert "May 2025" in md
    assert "a note" in md
