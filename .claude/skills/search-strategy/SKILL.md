---
name: search-strategy
description: What actually produces interview callbacks for Devanshu's search, from his own experience and from measured JobBot data. Use when deciding where to spend application slots, whether to raise volume, or what to build next.
---

# Search strategy

## What Devanshu has learned from a year of searching

- **Long applications don't produce callbacks.** Independently confirmed in JobBot's data: forms
  with 11+ fields completed at 19% vs 36% for 1-6. Both reasons point the same way, so they're
  skipped above 14 fields / 3 essays. Dream companies are exempt.
- **Workday applications DO produce callbacks** - his best-converting channel, and it wasn't
  being searched at all until 2026-09-25 (1490 Greenhouse, 1340 Ashby, 33 Lever, 0 Workday).
  They can't be auto-submitted (account per tenant), so they're worth his manual time in a way
  the long Greenhouse forms are not.

## What the measured data says

| Fact | Number |
|---|---|
| Ashby confirmed submissions | 46 |
| Greenhouse submissions, ever | **0** - reCAPTCHA Enterprise gates them |
| Responses so far | 1 rejection (Rogo) |
| Auto-acknowledgements | 7 (NOT responses) |

**Automation capacity is concentrated in Ashby.** Any plan that assumes Greenhouse volume is
wrong until the Enterprise CAPTCHA problem is solved, and it cannot be solved by circumventing
the bot check.

## On volume vs tailoring

Published 2026 figures claim tailored applications get roughly 3-5x the callback rate of generic
ones, and that bulk auto-appliers land at 2-5% against ~22% for manually customised
applications. **Treat the exact numbers sceptically** - almost every source selling these
statistics also sells a resume service. The directional claim is consistent enough to act on;
the magnitudes are not.

The honest tension: Devanshu wants ~100 applications/week, and the evidence says per-application
quality beats volume. JobBot currently sends **role-level variants**
(`product_design.pdf` / `design_engineer.pdf` / `ux_design.pdf`), not per-job tailored resumes.
Per-job tailoring exists in the spec and was never built - it is the highest-leverage unbuilt
piece, and `funnel_metrics()` already splits by resume kind so the two can be compared directly
once both are in use.

**Don't argue this from published statistics when his own data can settle it.** Ship per-job
tailoring, let both run, and read `jobbot metrics`. Require 20+ applications per group before
believing any difference.

## Where slots should go

1. Fresh postings first - age bucket outranks match score, because being early matters more than
   a few points of fit
2. Ashby, because it's the only ATS that actually submits
3. Dream companies always get filled but never auto-submitted
4. Workday surfaces for manual submission with the right resume variant named

## Things deliberately not done

- Defense and space primes are excluded: they gate on US-person status and clearance, which
  doesn't fit needing future sponsorship
- CAPTCHAs, bot checks and login walls are never circumvented
- Account creation is never performed, which is what makes Workday manual
