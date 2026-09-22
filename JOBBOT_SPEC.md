# Build Spec: JobBot — Local Automated Job Application Pipeline

You are building a personal job-application pipeline that runs on my own computer. Read this whole spec before writing any code. Build it in the phases below, in order, and run the checkpoint at the end of each phase before moving on. If something in this spec turns out to be wrong (an endpoint changed, a library behaves differently), tell me and adapt rather than silently working around it.

**What's already in this folder when you start:**
- `JOBBOT_SPEC.md` (this file)
- `bookmarks_9_21_26.html` — my browser bookmarks export. Only the **"Job Search"** folder matters; ignore every other folder and link. Parse it yourself and build `config/sources.yaml` from it, matching the table in Phase 1b.
- `resumes/` — several past resumes, including versions I made for different countries. Phase 3 explains how to use them.

**US jobs only.** Every job must be located in the United States or be remote within the US. Everything else is skipped.

**Goal for today:** a working pipeline that runs every hour on this machine, discovers new jobs from Greenhouse, Lever, and Ashby job boards, scores them, picks or tailors a resume, fills the application form, and (in REVIEW mode) waits for my approval before submitting. A daily email report summarizes everything. More sources and full AUTO mode come after it has run cleanly for a few days.

---

## 0. Hard constraints (never violate these)

1. **Zero extra cost.** No paid APIs, no paid services, no cloud servers. The only AI used is the local `claude` CLI (Claude Code) logged in with my Pro subscription.
2. **Never use an Anthropic API key.** When the code shells out to `claude`, it must build the subprocess environment with `ANTHROPIC_API_KEY` explicitly removed, even if it's not set. Add a startup check that warns loudly if that variable exists in the shell environment.
3. **Never fabricate resume content.** Tailored resumes may only reorder, rephrase, emphasize, or trim content that exists in the master resume. No new employers, titles, dates, degrees, certifications, metrics, or skills. This is enforced in code (see Phase 3 validator), not only in the prompt.
4. **Never bypass CAPTCHAs, bot checks, or login walls.** If one appears, stop that application, screenshot it, and mark it `needs_human`.
5. **Do not scrape LinkedIn, Indeed, Glassdoor, or any site that prohibits automation.** Those are handled later through job-alert emails (Phase 7).
6. **Secrets live in `.env`**, which is in `.gitignore`. Personal data never gets hardcoded.
7. **Be a polite client.** One browser at a time, randomized 3–10 second pauses between page actions that submit data, a max of `MAX_APPS_PER_RUN` (default 5) and `MAX_APPS_PER_DAY` (default 30).

---

## 1. Tech stack

- Python 3.11+
- `httpx` for HTTP, `playwright` (Chromium) for forms and PDF rendering, `sqlite3` (stdlib) for state
- `pyyaml`, `python-dotenv`, `jinja2`, `rapidfuzz` (fuzzy title/question matching), `beautifulsoup4` (strip HTML from job descriptions), `typer` (CLI)
- Resume PDFs: render resume data → Jinja2 HTML template → `page.pdf()` in Playwright. No other PDF libraries.
- Use a virtual environment in the project folder. Provide a `requirements.txt` and a one-command setup script.

First, detect the operating system (macOS, Windows, or Linux) and tell me what you found. Everything OS-specific (paths, scheduler, power settings) must follow that.

---

## 2. Project layout

```
jobbot/
  .env                      # secrets (gitignored)
  .env.example
  config/
    profile.yaml            # my personal info + standard answers
    search.yaml             # roles, keywords, filters, thresholds
    companies.yaml          # company board list, favorites, dream list, blocklist
    sources.yaml            # aggregator/alert sources from my bookmarks (Phase 1b)
    keywords.yaml           # skill/tool taxonomy for JD analysis (Phase 6b)
  bookmarks_9_21_26.html    # my bookmarks export (only the "Job Search" folder is used)
  resumes/
    *.pdf / *.docx          # my existing resumes, incl. country-specific versions (read-only, never edit)
    master.json             # structured, US-format master you build from ALL of them
    experience_library.json # every distinct bullet, project, and skill found across all versions
    conflicts.md            # anything that disagrees between versions, for me to resolve
    variants/               # product_design.json, design_engineer.json, ux_design.json + PDFs
    tailored/               # {company}_{job_id}.json + .pdf
    templates/resume.html.j2
  jobbot/
    cli.py
    db.py
    discover/               # greenhouse.py, lever.py, ashby.py, base.py
    score.py
    resume/                 # parse.py, variants.py, tailor.py, validate.py, render.py
    apply/                  # base.py, greenhouse.py, lever.py, ashby.py, answers.py
    claude_cli.py           # the ONLY place that calls the claude binary
    report.py
    ingest/                 # email_alerts.py, aggregators.py, resolve_ats.py
    outcomes/               # email_classifier.py, metrics.py, monthly.py
    scheduler/              # install scripts per OS
  data/jobbot.db
  logs/                     # rotating logs
  screenshots/
  tests/
```

---

## 3. Config files (create with examples; I'll fill them in)

### `config/profile.yaml`
Personal info: full name, preferred name, email, phone, city/state/country, LinkedIn, portfolio URL, GitHub, personal site. Work authorization (authorized in US yes/no, needs sponsorship now/future). Willing to relocate, remote/hybrid/onsite preference, earliest start date, notice period, desired salary (min and target), pronouns (optional).

Standard answers keyed by question type: how did you hear about us (default "Company website"), years of experience overall and per skill, highest degree, school, graduation year, previously worked here (default no), related to employees (default no), over 18 (yes).

EEO/demographic questions (gender, race, veteran, disability): default to the "decline to self-identify" option. I can override per field.

A `cover_letter_policy`: `skip_if_optional` (default), and a short reusable cover note template.

### `config/search.yaml`
```yaml
roles:
  product_design:
    titles_exact: ["Product Designer", "Senior Product Designer", "Product Design Lead"]
    titles_related: ["Interaction Designer", "Digital Product Designer"]
  design_engineer:
    titles_exact: ["Design Engineer", "UX Engineer", "Design Technologist"]
    titles_related: ["Frontend Engineer, Design Systems", "Creative Technologist", "UI Engineer"]
  ux_design:
    titles_exact: ["UX Designer", "UX/UI Designer", "User Experience Designer"]
    titles_related: ["UI Designer", "Experience Designer"]
title_blocklist: ["Intern", "Graphic Designer", "Instructional Designer", "Interior", "Fashion"]
seniority:
  target: mid
  allowed_title_words: []            # plain titles, e.g. "Product Designer", count as mid
  soft_title_words: ["Senior"]       # allowed but scored lower; set to [] to skip Senior roles
  blocked_title_words: ["Intern", "Internship", "Junior", "Jr", "Entry", "Associate", "Apprentice",
                        "Staff", "Principal", "Lead", "Head of", "Manager", "Director", "VP"]
  years_required: { min_ok: 2, max_ok: 6 }   # "0-1 years" or "8+ years" fail the gate
salary:
  currency: USD
  target_min: 130000
  target_max: 200000
  hourly_to_annual_hours: 2080
  if_unlisted: allow_with_penalty    # allow_with_penalty | skip
  unlisted_penalty: 10
locations:
  country: US                 # hard requirement, US only
  remote_us_ok: true          # remote roles count only if open to US-based candidates
  cities: []                  # optional preferred US cities (bonus points, not a filter)
  # Skip: non-US locations, "remote" limited to other countries/regions (e.g. "Remote - EMEA",
  # "Remote (Canada)", "UK only"), and postings requiring residence outside the US.
thresholds: { skip_below: 50, tailor_at_or_above: 75 }
limits: { max_tailors_per_day: 10, max_apps_per_run: 5, max_apps_per_day: 30 }
mode: REVIEW        # DRY_RUN | REVIEW | AUTO
report_time: "20:00"
tailor_batch_hours: [9, 13, 17]
```

### `config/companies.yaml`
A list of companies, each with `name`, `ats` (greenhouse | lever | ashby), `board_token`, and optional `favorite: true`, `dream: true`. Plus a `blocklist` of companies never to apply to. Also include an `unsupported` list for sites I check that aren't on those ATSs; the pipeline just logs new postings from those for me to handle manually if you can get them from a public feed, otherwise skip.

Write a helper command `jobbot find-board "<company careers URL>"` that visits a careers page and detects whether it's Greenhouse, Lever, or Ashby and what the board token is, so I can build this list quickly.

### `.env`
`GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `REPORT_TO`, `TIMEZONE`.

---

## 4. Database (SQLite)

Tables at minimum:
- `jobs`: id, source_ats, company, board_token, external_id, title, location, remote flag, url, apply_url, description_text, posted_at, first_seen_at, content_hash. Unique on (source_ats, board_token, external_id).
- `scores`: job_id, total, title_pts, skills_pts, seniority_pts, location_pts, company_bonus, role_bucket, tier (skip | variant | tailor | dream_review), reasons (JSON).
- `resumes_used`: job_id, kind (variant | tailored), path_json, path_pdf, created_at.
- `applications`: job_id, status (queued | filled_awaiting_review | submitted | failed | needs_human | skipped), attempts, last_error, screenshot_path, filled_answers (JSON), submitted_at.
- `claude_calls`: timestamp, purpose, job_id, success, duration, error.
- `runs`: start, end, counts per stage, errors.
- `jobs` also stores: source (which bookmark/source found it), salary_min, salary_max, salary_currency, salary_source (structured | text | unlisted), years_required, seniority_label.
- `jd_keywords`: job_id, keyword, category (tool | skill | method | domain), found_at. Filled for EVERY analyzed job, including skipped ones, so market trends are measured on everything seen.
- `outcomes`: application_id, stage, occurred_at, evidence (email subject/sender or "manual"), confidence (rule | claude | manual).
  Stages, in funnel order: `submitted` → `acknowledged` → `rejected` | `assessment` → `recruiter_screen` → `interview` → `final_round` → `offer` → `accepted` | `declined`. Plus `no_response` (set automatically after 21 days of silence; configurable).
- `processed_emails`: message_id, purpose, processed_at.
- `recommendations`: month, text, status (proposed | accepted | dismissed).

Use a lock file so two runs never overlap. If a run is still going when the next hour starts, the new one exits cleanly.

---

## Phase 1 — Discovery

Implement one fetcher per ATS using their public job-board endpoints (verify each still works before relying on it):
- Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
- Lever: `https://api.lever.co/v0/postings/{company}?mode=json`
- Ashby: `https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true`

Normalize every posting into the `jobs` schema, strip HTML from descriptions, dedupe, and only treat unseen jobs as new. Handle timeouts and bad responses per company without failing the whole run.

**Checkpoint 1:** `jobbot discover` against 3 real companies I list; print counts of total and new jobs, plus 5 sample normalized records.

## Phase 1b — My actual sources (from my browser bookmarks)

These are the job-search links I check by hand today. Handle each one as described. Before automating any site, read its terms of service and robots.txt; if automated access is prohibited or unclear, use the fallback (email alerts or the "check manually" list in the daily report) and tell me.

| Source | Link | How to handle |
|---|---|---|
| LinkedIn "New Roles 3 hours" (Product Designer, US) | `https://www.linkedin.com/jobs/search/?distance=25&f_E=1%2C2%2C3&f_TPR=r7200&geoId=103644278&keywords=Product%20Designer` | **Never scrape or automate LinkedIn.** I'll create LinkedIn job alerts instead (see note below). Read the alert emails via Gmail IMAP. |
| LinkedIn "New Roles 6 hours" (UX Designer, US) | same pattern, `keywords=UX%20Designer`, `f_TPR=r21600` | Same as above. |
| Brian's Job Search | `https://briansjobsearch.com/?job=Product+Designer&time=24hours` | Aggregator. If allowed, read listings; follow each apply link. |
| HiringCafe (US) | `https://hiring.cafe/?searchState={"searchQuery":"ux designer"}` | Aggregator. If allowed, read listings; follow each apply link. |
| HiringCafe (UK, India, Singapore, Dubai) | same site, location-filtered | **Do not use.** US jobs only. Leave these out of `sources.yaml`. |
| Y Combinator jobs | `https://www.ycombinator.com/jobs/role/designer` | Discovery only. YC startups often use Ashby/Greenhouse/Lever; if a posting's apply link goes to one of those, handle normally. Anything that applies inside Work at a Startup → "check manually" list. |
| Wellfound | `https://wellfound.com/jobs` | Applies happen inside Wellfound with an account → do not automate. Discovery via their email digests only; list in report. |
| Open Doors | `https://www.opendoorscareers.com/?job=Internship&country=US` | My bookmark is filtered to **internships**, which conflicts with mid-level. Disable until I give a mid-level link. |
| UIUXjobsboard | `https://uiuxjobsboard.com/c/design-internships` | Also filtered to **internships**. Disable until I give a mid-level link. |

**The "resolve to ATS" strategy (use it for every aggregator and email source):**
1. From each listing, get the company name, job title, and outbound apply link.
2. If the link points to Greenhouse, Lever, or Ashby, extract the board token, **automatically add the company to `companies.yaml`** (marked `source: auto`), and process the job through the normal pipeline. My company list grows by itself over time.
3. If there's no usable link (e.g. LinkedIn alert emails, which link back to LinkedIn), try to resolve the company's board by probing the public endpoints with slug guesses from the company name (`acme-inc`, `acmeinc`, `acme`). If found, look for a posting on that board whose title fuzzy-matches the alert, and process that one. Never open the LinkedIn link itself.
4. If none of that works, put the job in the report's "check manually" list with its link.

**LinkedIn alert setup note (for me):** my current bookmarks use `f_E=1,2,3` (internship, entry, associate). For mid-level roles the alerts should use the Mid-Senior level filter. Remind me of this and tell me the exact settings when you reach this phase.

**Email ingestion (`jobbot/ingest/email_alerts.py`):** connect to Gmail over IMAP with the same app password used for reports. Only read messages from known alert senders (LinkedIn, Wellfound, HiringCafe, etc., configurable), and mark processed message IDs in the DB so nothing is read twice.


---

## Phase 2 — Scoring (pure code, no AI)

**Hard gates first (a failed gate means tier `skip`, with the reason stored):**
- **Mid-level gate:** title contains a `blocked_title_words` entry → skip. A required-years phrase ("5+ years", "3-5 years of experience") outside `years_required` → skip. `soft_title_words` (e.g. Senior) pass the gate but get fewer seniority points.
- **Salary gate:** parse compensation from structured fields first (Ashby compensation data, Greenhouse pay ranges when present), then from the description text with regex covering formats like "$130,000 – $180,000", "$130K-$180K", "130k to 180k", and hourly rates (convert with `hourly_to_annual_hours`). If a range is found: pass only if it overlaps 130K–200K (range max ≥ 130K and range min ≤ 200K). A range whose minimum is above 200K almost always signals a senior/staff role → skip. If no salary is listed → follow `if_unlisted` (default: allow, subtract `unlisted_penalty`, and tag the job `salary_unlisted`). Store parsed min/max/currency/source on the job.
- **US location gate:** run before everything else. Parse the location field and the description; pass only US locations or remote roles open to US-based candidates (see `locations` in `search.yaml`). Non-USD salary ranges are treated as non-US and skipped.

Then score 0–100:
- **Title (0–40):** fuzzy match (rapidfuzz) against each role's `titles_exact` (full points) and `titles_related` (partial, ~20). Any blocklist hit → score 0 and tier `skip`. The best-matching role sets `role_bucket`.
- **Skills (0–30):** overlap between the job description and a keyword bank built from `master.json` (tools, skills, methods). Points proportional to matched keywords, capped.
- **Seniority (0–15):** parse title words (Junior, Senior, Staff, Lead, Principal) and "X+ years" phrases; full points if it fits my level, partial if one step off, zero if far off.
- **Location (0–10):** remote/city/country fit.
- **Company bonus:** +10 favorite, +15 dream. Blocklisted company → skip.

Tiers: below `skip_below` → `skip`; between → `variant`; at or above `tailor_at_or_above` → `tailor`; dream company with score > `skip_below` → `dream_review` (tailor and always hold for review, even in AUTO mode).

Store the reasons so the report can explain each decision.

**Checkpoint 2:** `jobbot score --explain` prints the top 15 new jobs with their breakdown.

---

## Phase 3 — Resumes

1. **Learn from all my past resumes, then build one US master (one-time).** The `resumes/` folder has several resumes, including versions I made for other countries. They're there so you can learn everything I've done, not to be sent as-is.
   - Extract text from every PDF/DOCX in `resumes/` with code first. Then use one `claude -p` call per resume to structure it into the same schema (contact, summary, experience with bullets, projects, education, skills, certifications, links, tools).
   - Merge them in code into `experience_library.json`: every distinct role, bullet, project, metric, and skill, deduplicated (fuzzy-match near-identical bullets), each tagged with which file(s) it came from.
   - Detect conflicts between versions (different dates, titles, degree names, or metrics for the same item) and write them to `conflicts.md`. **Stop and ask me to resolve them.** Never guess which version is right.
   - Build `master.json` from the resolved library in **US resume format**: no photo, date of birth, age, marital status, nationality, gender, or visa/passport details in the resume itself; US spelling (e.g. "optimize", "color"); "Month YYYY" dates; US phone format; 1 page if under ~6 years of experience, max 2 pages; strongest content first. International roles and education stay in, stated plainly with city and country.
   - Show me `master.json` and a rendered PDF of it for approval before anything else in Phase 3 runs.
   - **The fact boundary** for everything later (variants, tailoring, form answers, the validator) is the **whole resolved experience library**, not just what fits on the master. That lets tailoring pull in a relevant past bullet that didn't make the master, but never anything that isn't in one of my real resumes.
2. **Generate three role variants** (one-time, 3 Claude calls): product_design, design_engineer, ux_design. Each reorders sections, rewrites the summary, and reprioritizes bullets and skills for that role.
3. **Tailoring** for `tailor` and `dream_review` jobs: one Claude call per job, given `master.json`, the role variant, and the job description. Output must be JSON in the same schema.
4. **Validator (`validate.py`), mandatory for every Claude output:** reject if any employer, title, date, school, degree, or certification is not in the resolved experience library; reject any skill not in the library's keyword bank; reject numbers/metrics that don't appear in the library; reject any country-specific personal details listed above. On rejection, retry once with the errors listed; if it fails again, fall back to the role variant and log it.
5. **Render** to a clean, ATS-friendly single-column PDF via the Jinja2 template (no tables for layout, no images, real text). File name: `Firstname_Lastname_Resume_{Company}.pdf`.

### `claude_cli.py`
- Calls `claude -p` with the prompt via stdin and `--output-format json`, with a timeout (e.g. 180s), using an environment copy with `ANTHROPIC_API_KEY` removed.
- Parses the result; extracts JSON from the model's text (strip code fences).
- Detects usage-limit or auth errors and raises a specific `ClaudeUnavailable` so callers fall back instead of crashing.
- Logs every call to `claude_calls`.
- Enforces `max_tailors_per_day`; beyond that, callers use the variant.
- Tailoring only runs during `tailor_batch_hours`; jobs that need tailoring wait until then. Everything else runs hourly.

**Checkpoint 3:** show me `conflicts.md` (resolved), `master.json`, the three variant PDFs, and one tailored PDF plus the validator's report.

---

## Phase 4 — Applying

Build a Playwright adapter per ATS (Greenhouse, Lever, Ashby) sharing a base class:
- Open the apply URL, detect the form, fill standard fields from `profile.yaml`, upload the chosen PDF, and handle the common custom questions.
- **Question matching (`answers.py`):** normalize each question label and fuzzy-match against known question types in `profile.yaml`. For dropdowns and radio buttons, pick the option that best matches the configured answer. EEO fields default to decline.
- **Unknown required questions:** if it's a short free-text "why this company/role" type, draft an answer with one Claude call (counts toward the daily cap; must only reference facts from `master.json` and the job description). Anything else unknown → `needs_human`.
- **Detection:** CAPTCHA, login required, account creation, or unexpected page → stop, screenshot, `needs_human`.
- **Modes:**
  - `DRY_RUN`: fill everything, screenshot the filled form, never click submit.
  - `REVIEW` (default): fill, screenshot, save all answers to `applications.filled_answers`, status `filled_awaiting_review`. `jobbot review` lists them with screenshots; `jobbot approve <id|all>` re-opens and submits; `jobbot reject <id>` marks skipped.
  - `AUTO`: submit directly, except `dream_review` jobs which always wait.
- After submit, verify a confirmation message or URL change; otherwise mark `failed` with a screenshot.
- Run headless by default; `--headed` flag for debugging.

**Checkpoint 4:** DRY_RUN on one real posting from each ATS; show me the screenshots.

---

## Phase 5 — Orchestration, scheduling, keeping the machine awake

`jobbot run` does: discover → score → assign resumes (variant now, tailoring if in a batch hour) → fill/submit within limits → record the run. Exit code 0 even when individual jobs fail.

Install an hourly schedule for the detected OS:
- **macOS:** a `launchd` plist in `~/Library/LaunchAgents` with `StartInterval` 3600, logs to `logs/`. Tell me the exact `pmset` / System Settings steps so the Mac doesn't sleep while plugged in (display sleep is fine).
- **Windows:** a Task Scheduler task via `schtasks` or a PowerShell script, hourly, "run whether user is logged on or not" where possible, with "wake the computer to run this task" enabled. Give me the exact power settings to change.
- **Linux:** a systemd user timer (preferred) or cron, plus sleep settings guidance.

Also: `jobbot install-schedule`, `jobbot uninstall-schedule`, and `jobbot status` (last run time, next run, today's counts, whether Claude CLI is reachable).

Missed runs while the computer is asleep should simply be skipped; the next run catches up because discovery is based on "unseen," not time windows.

---

## Phase 6 — Daily report

At `report_time`, send an HTML email via Gmail SMTP (app password) and save a copy to `logs/reports/YYYY-MM-DD.html`:
- Counts: discovered, new, skipped, variant, tailored, filled awaiting review, submitted, failed, needs_human, Claude calls used vs cap.
- Table of submitted applications (company, role, score, resume used, link).
- "Needs you" section: awaiting review and needs_human, with the reason and link.
- Top skip reasons, so I can tune thresholds.
- Any errors from the day's runs.

Schedule this as its own daily job. `jobbot report --now` sends it immediately for testing.

---

## Phase 6b — Outcome tracking, learning, and monthly progress

The point of this phase: every job description analyzed and every application sent becomes data, so each month we can see what's working and adjust.

**A. Job-description analysis (every job, no AI).**
Maintain a keyword taxonomy in `config/keywords.yaml` (tools like Figma, ProtoPie, React, TypeScript; skills like prototyping, design systems, usability testing, accessibility; domains like fintech, healthcare, B2B SaaS), seeded from my master resume plus common design/design-engineering terms. Extract matches from every job description into `jd_keywords`. When a term appears often in descriptions but isn't in the taxonomy (simple n-gram frequency), add it to a "candidate terms" list in the monthly report for me to approve.

**B. Capturing callbacks (free, mostly rule-based).**
- Read Gmail over IMAP (same app password). Match incoming emails to applications by sender domain, company name, and job title.
- Classify with rules first: rejection phrases ("unfortunately", "decided to move forward with other candidates", "not moving forward"), interview/screen signals (scheduling links such as Calendly, GoodTime, or the ATS's own scheduler; "schedule a call", "next steps"), assessments ("take-home", "design challenge", "exercise"), offers ("offer letter", "pleased to offer"). ATS confirmation emails count as `acknowledged`.
- Emails the rules can't classify go into one batched `claude -p` call per day (counts toward the Claude cap), sending only the subject, sender, and first ~500 characters. Store the result with `confidence: claude`.
- Manual override: `jobbot outcome <application_id> <stage> [--note "..."]`, and `jobbot outcomes --pending` to list matches the pipeline is unsure about.
- Never reply to, archive, or modify any email. Read-only.

**C. Portfolio view signal (optional, free).**
Put a UTM-tagged portfolio link in each application (`?utm_source=jobbot&utm_campaign={company_slug}`) so my portfolio's analytics can show which companies actually looked. Just generate the links; I'll read the analytics myself unless we wire up the free GA4 Data API later.

**D. Metrics (computed from the DB, no AI).**
- Funnel: applied → any response → screen → interview → final → offer, as counts and rates.
- Median days to first response.
- Response and interview rates broken down by: role bucket, resume kind (variant vs tailored), score band (50–64, 65–74, 75–84, 85+), salary listed vs unlisted, salary band, source (direct board, aggregator, email alert), company favorite/dream vs other, weekday applied.
- Market view from `jd_keywords`: top 20 requested skills per role this month, biggest risers and fallers vs last month, and a **gap list** of frequently requested skills that are not in my master resume. The gap list is for me to review: only add a skill to my resume if I genuinely have it.
- Salary view: distribution of listed salary ranges for mid-level roles in my three tracks.
- Always show sample sizes. Don't call a difference meaningful unless each group has at least 20 applications; label smaller comparisons "too early to tell."

**E. Monthly report and dashboard.**
- On the 1st of each month, email a monthly report and save it as `logs/reports/monthly/YYYY-MM.html`: this month vs last month for every metric, simple charts (matplotlib PNGs embedded inline), the market view, and 3–5 recommendations.
- Recommendations come from one `claude -p` call that receives only the aggregated numbers (never raw emails). Examples: "tailored resumes got 2× the interview rate of variants, raise the daily tailoring cap," "salary-unlisted jobs almost never respond, switch `if_unlisted` to skip," "Design Engineer postings increasingly mention TypeScript." Each one is saved in `recommendations`.
- Recommendations never change config on their own. `jobbot tune` lists them; `jobbot tune --accept <id>` applies the config change it describes.
- `jobbot dashboard` regenerates a local `logs/dashboard.html` with all-time and month-to-date numbers.
- Add to the daily report: callbacks received today (by stage) and a running month-to-date funnel.

**Checkpoint 6b:** load 30 fake applications and 15 fake emails as fixtures, run classification, and show me a generated monthly report.

## Phase 7 — Later (do NOT build today, just leave clean extension points)

- Workable and SmartRecruiters adapters.
- Automated networking drafts (outreach to recruiters/designers at applied companies), always human-approved before sending.

---

## Testing and quality

- Unit tests for scoring, question matching, and the resume validator (include a test where the model invents a skill and must be rejected).
- Save sample API responses as fixtures so tests don't hit the network.
- Structured logging with rotation. Every exception records job id and stage.
- A `README.md` with setup, config, commands, and troubleshooting.

## Definition of done for today

1. `jobbot run` works end to end in REVIEW mode on real Greenhouse, Lever, and Ashby postings.
2. Three variant PDFs exist and look good; at least one tailored PDF passed the validator.
3. The hourly schedule and the daily report are installed and verified (`jobbot report --now` delivered an email).
4. `jobbot review` and `jobbot approve` work.
5. Mid-level and salary gates are active, and skip reasons show up in the report.
6. Every analyzed job writes to `jd_keywords`; the Gmail outcome reader runs (even if there are no callbacks yet) and `jobbot outcome` works manually.
7. At least one aggregator from Phase 1b is feeding jobs through the "resolve to ATS" path, or you've told me why it isn't allowed.
8. You've told me exactly what I need to fill in or change, and anything that didn't work.

Start by detecting my OS, checking that Python 3.11+ and the `claude` CLI are available, confirming `ANTHROPIC_API_KEY` is not set, and confirming you can see `bookmarks_9_21_26.html` and the files in `resumes/`. List the Job Search links you found and the resumes you found, ask me for any companies I want to add to `companies.yaml`, then begin Phase 1.
