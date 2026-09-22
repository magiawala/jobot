"""Decides which `tailor`/`dream_review` jobs get an actual Claude-written tailored resume
today, given the `max_tailors_per_day` cap. Everything not selected falls back to the plain
role variant (same fallback path used when validation fails or Claude is unavailable).

Per Devanshu's instruction: prioritize customization effort on jobs where it matters most -
reputable/larger companies, roles with a weak natural keyword overlap (a strong overlap means
the generic variant probably already reads well), and roles paying toward the top of the
target range - not on every tailor-tier job indiscriminately.

Signals used, all free / already in the DB - no paid company-size APIs, no LinkedIn:
  - dream/favorite flag from companies.yaml ("good companies")
  - total open postings on the company's board, as a size/activity proxy ("more employees")
  - skills_pts from scoring, inverted - LOW keyword overlap scores HIGHER priority here
  - salary_max relative to the target range - higher pay scores higher priority
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass
class PriorityInputs:
    job_id: int
    skills_pts: int  # 0-30, from scores.skills_pts
    salary_max: float | None
    is_dream: bool
    is_favorite: bool
    company_total_postings: int
    base_total: int  # scores.total, used only as a small tiebreaker


def company_size_score(total_postings: int, cap: int = 25) -> float:
    """Log-scaled: a 600-req company (Stripe) shouldn't dominate 40x over a 15-req one."""
    if total_postings <= 0:
        return 0.0
    return min(cap, math.log10(total_postings + 1) * (cap / 3.0))


def keyword_gap_score(skills_pts: int, cap: int = 20, max_skills_pts: int = 30) -> float:
    """Inverted on purpose: a LOW skills_pts (weak natural JD/resume overlap) means tailoring
    has more work to do and more value to add, so it scores HIGHER, not lower."""
    gap = max(0, max_skills_pts - skills_pts)
    return min(cap, gap / max_skills_pts * cap)


def salary_priority_score(salary_max: float | None, target_min: float, target_max: float, cap: int = 15) -> float:
    if not salary_max:
        return 0.0
    span = max(1.0, target_max - target_min)
    return min(cap, max(0.0, (salary_max - target_min) / span * cap))


def tailoring_priority(p: PriorityInputs, target_min: float = 130_000, target_max: float = 200_000) -> float:
    company_bonus = 30.0 if p.is_dream else (15.0 if p.is_favorite else 0.0)
    return (
        company_bonus
        + company_size_score(p.company_total_postings)
        + keyword_gap_score(p.skills_pts)
        + salary_priority_score(p.salary_max, target_min, target_max)
        + p.base_total / 10.0  # small tiebreaker so a strong all-around match still edges out a weak one
    )


def rank_for_tailoring(candidates: list[PriorityInputs], max_tailors_today: int,
                       target_min: float = 130_000, target_max: float = 200_000) -> tuple[list[int], list[int]]:
    """Returns (job_ids_to_tailor, job_ids_falling_back_to_variant), highest priority first.
    dream_review jobs are never in `candidates` filtered out here - the caller should always
    include them since the spec holds them for review regardless of tailoring depth."""
    scored = sorted(candidates, key=lambda p: -tailoring_priority(p, target_min, target_max))
    chosen = [p.job_id for p in scored[:max_tailors_today]]
    rest = [p.job_id for p in scored[max_tailors_today:]]
    return chosen, rest
