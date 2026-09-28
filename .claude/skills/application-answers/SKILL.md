---
name: application-answers
description: How to answer job-application questions on Devanshu's behalf - what may be answered from his profile, what may be drafted, and what must never be guessed. Use when extending the answer matcher, the drafter, or the learned-answers queue, or when an application is blocked on a question.
---

# Answering application questions

These go onto real job applications under Devanshu's name. A wrong answer is a
misrepresentation, not a bug - which makes the failure modes asymmetric and shapes every rule
below.

## The resolution order

1. **Profile match** (`classify_label`) - a known question type answered from `profile.yaml`
2. **Derivation** (`derive_answer`) - implied by the profile, e.g. "Do you live in one of these
   states? Alabama, Alaska..." is answerable from his state
3. **Learned answer** - something he answered once before, fuzzy-matched so wording drift hits
4. **Claude draft** - open-ended questions only, grounded in `master.json`
5. **Otherwise: leave it unanswered.** The application goes to review. That is a correct
   outcome, not a failure.

## Never guess these

Hard-coded as always-UNKNOWN, whatever the question shape:

- citizenship, country of birth, permanent residency
- security clearances, export-control eligibility
- criminal history, whether he's been fired or asked to resign
- GPA, salary history, salary comfort with a posted range
- visa specifics beyond the tri-state work-authorization fields

`work_authorization` is deliberately **tri-state**. A null must never collapse to "No" - that
once answered "Are you authorized to work in the US?" with No on live applications.

## Drafting

Decided by **signal, not a list of phrasings** - every posting words these differently ("Why
Levelpath?", "What is it about Gamma that made you apply?", "Pitch me one bold idea..."), and
enumerating them never converges.

- Draftable: open-ended prompts asking for explanation, opinion or an example
- Not draftable: closed-form yes/no and single-value questions, and **anything needing a fact we
  don't hold** - that guard runs FIRST, so "What was your undergraduate GPA?" is excluded even
  though it's shaped like an open question
- Normalize the label before matching: curly apostrophes silently defeat ASCII patterns

Drafts must use only `master.json` facts. No invented employers, metrics or claims about the
company.

## Matching traps that have actually bitten

- `partial_ratio` scores the best matching SUBSTRING: "exceptional **ability**" hit "dis**ability**"
  at 82% and wrote the EEO answer "decline" into a free-text box. EEO phrases now require a word
  boundary.
- "how did you hear about this opportunity" matched `location` at 85% and would have typed
  "Boston, MA" into it.
- "Name Pronunciation | How do you pronounce your name?" matched the name patterns and got the
  legal name.
- Fuzzy-matching a dropdown at 70% picked "University of Indianapolis" for "Indiana University
  Indianapolis" - a different school. Consequential fields (school, degree, work auth, employer)
  need a near-exact match or no answer.

## The learned-answers queue

Anything unanswerable is recorded once with its ATS, options and an example posting.
`jobbot learn --auto` clears it in three passes, cheapest first:

1. drop entries that are an option label, not a question (Greenhouse records each checkbox
   choice separately, so "Instagram", "Monkey MindPong" and "Neuralink Show & Tell" arrive as
   questions). Detect structurally - short, no question mark, not phrased as a question - since
   every company invents its own.
2. clear anything the code already answers - no Claude call needed
3. batch the rest to Claude with the profile and resume as the ONLY permitted source

It went 118 pending -> 13 on 7 calls. Its restraint is the point: it declined "have you entered
into a non-disclosure agreement" rather than answering No, which would likely have been false.

## Stale records don't self-heal

Every time a class of question becomes answerable, applications already parked in `needs_human`
stay parked. The highest-scoring job in the database sat blocked for days on a question that had
been answerable the whole time. `revive_stale()` runs hourly for this; keep it conservative -
revive only when EVERY blocker resolves.
