from jobbot.resume.tailor_priority import PriorityInputs, rank_for_tailoring, tailoring_priority


def make(job_id, skills_pts=15, salary_max=170_000, dream=False, favorite=False, size=20, total=80):
    return PriorityInputs(job_id=job_id, skills_pts=skills_pts, salary_max=salary_max,
                          is_dream=dream, is_favorite=favorite, company_total_postings=size, base_total=total)


def test_dream_company_outranks_plain_company_otherwise_equal():
    dream = make(1, dream=True)
    plain = make(2)
    assert tailoring_priority(dream) > tailoring_priority(plain)


def test_weak_keyword_match_outranks_strong_match_otherwise_equal():
    weak_match = make(1, skills_pts=2)   # low natural overlap -> tailoring adds more value
    strong_match = make(2, skills_pts=28)  # already matches well -> variant is probably fine
    assert tailoring_priority(weak_match) > tailoring_priority(strong_match)


def test_higher_salary_outranks_lower_salary():
    high_pay = make(1, salary_max=195_000)
    low_pay = make(2, salary_max=135_000)
    assert tailoring_priority(high_pay) > tailoring_priority(low_pay)


def test_bigger_company_outranks_smaller_otherwise_equal():
    big = make(1, size=600)
    small = make(2, size=3)
    assert tailoring_priority(big) > tailoring_priority(small)


def test_rank_for_tailoring_respects_daily_cap():
    candidates = [make(i, skills_pts=i) for i in range(1, 11)]  # 10 candidates, varying keyword gap
    chosen, rest = rank_for_tailoring(candidates, max_tailors_today=3)
    assert len(chosen) == 3
    assert len(rest) == 7
    assert set(chosen) | set(rest) == {c.job_id for c in candidates}
    # lowest skills_pts (weakest match) should be prioritized first
    assert chosen[0] == 1


def test_unlisted_salary_does_not_crash_and_scores_neutrally():
    p = make(1, salary_max=None)
    assert tailoring_priority(p) >= 0
