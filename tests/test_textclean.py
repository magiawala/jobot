from jobbot.resume.textclean import normalize_resume_text, normalize_text


def test_em_dash_with_spaces_becomes_comma():
    assert normalize_text("owns end-to-end design — research, prototyping") == \
        "owns end-to-end design, research, prototyping"


def test_bare_em_dash_becomes_hyphen():
    assert normalize_text("Jan 2023—Present") == "Jan 2023-Present"


def test_en_dash_becomes_hyphen():
    assert normalize_text("pages 1–2") == "pages 1-2"


def test_curly_quotes_become_straight():
    assert normalize_text("Master’s degree") == "Master's degree"
    assert normalize_text("“quoted”") == '"quoted"'


def test_ellipsis_character_becomes_three_periods():
    assert normalize_text("and so on…") == "and so on..."


def test_no_double_comma_after_dash_swap():
    assert normalize_text("design —, research") == "design, research"


def test_plain_ascii_text_is_unchanged():
    s = "Shipped a redesign that lifted conversion by 12% - a straightforward win."
    assert normalize_text(s) == s


def test_normalize_resume_text_recurses_through_nested_structure():
    resume = {
        "summary": "Owns the process — start to finish",
        "experience": [{"company": "Acme", "bullets": ["Cut costs — 20% savings", "Plain bullet"]}],
        "skills": {"Design": ["Figma"]},
    }
    out = normalize_resume_text(resume)
    assert out["summary"] == "Owns the process, start to finish"
    assert out["experience"][0]["bullets"][0] == "Cut costs, 20% savings"
    assert out["experience"][0]["bullets"][1] == "Plain bullet"
    assert out["skills"] == {"Design": ["Figma"]}
