---
name: ats-playbook
description: Hard-won facts about how Greenhouse, Ashby, Lever and Workday application forms actually behave, and which ones can be submitted automatically at all. Use when writing or debugging JobBot's apply adapters, or when a submission fails, fills wrongly, or appears to succeed without landing.
---

# ATS playbook

Every item here was learned by breaking against a live posting. Treat it as measurement, not
guesswork, and re-measure before assuming any of it still holds.

## Which ATSs can actually be submitted to

| ATS | Automatable | Evidence |
|---|---|---|
| **Ashby** | Yes | 46 confirmed submissions |
| **Greenhouse** | **No, where reCAPTCHA Enterprise is present** | 0 submissions ever, 0 replies |
| **Lever** | Partly | location autocomplete doesn't resolve headless |
| **Workday** | No | requires an account per company tenant |

**Greenhouse + reCAPTCHA Enterprise is the single biggest trap.** The form fills perfectly,
`form.checkValidity()` is true, the submit button is enabled, and nothing looks wrong. Then:

- `boards.greenhouse.io` -> POST reaches the server, returns **428**
- `job-boards.greenhouse.io` -> the client **never sends the request at all**

Detect it by the `recaptcha/enterprise/anchor` iframe, before filling. Never try to solve or
work around a bot check; route to a human.

## Per-ATS structure

**Greenhouse**
- Stable ids: `#first_name #last_name #email #phone #candidate-location #resume`
- Custom questions are `#question_<per-job-id>` -> match by LABEL text, never by id
- Required is signalled by `*` in the label, NOT the `required` attribute
- Comboboxes are react-select: a visible text input plus a **hidden required input with no name,
  no id and no label**. Typing into the visible box leaves the hidden one empty -> invalid form
  -> silent submit failure. Always verify with `form.checkValidity()`.
- A page-global `[role=option]` query also matches the international phone widget's country
  list, so a dropdown will happily read "Afghanistan +93" as its options.

**Ashby**
- `#_systemfield_name` is a SINGLE full-name field, plus `#_systemfield_email`, `#_systemfield_resume`
- Field ids are UUIDs, often starting with a digit -> use `[name=]`/`[id=]`, never `#id`
- Yes/no questions are `<button data-option>` pairs with `aria-pressed` - **invisible to any
  input/textarea/select scan**
- Radio and checkbox groups live in a `<fieldset>`; each input's own label is the OPTION text,
  never the question. Read the question from the fieldset.
- Required is a CSS `::after { content: "*" }` on the label, which `innerText` does NOT include
- Location is `input[role=combobox]`; select with ArrowDown+Enter, not by clicking an option
  (a `[class*=option]` click target also matches the yes/no buttons)
- The SMS-consent radio sits inside the Phone field entry, so leaving it blank makes the whole
  Phone group read as empty

**Lever**
- Semantic names: `name`, `email`, `phone`, `location`, `org`, `urls[LinkedIn]`
- Custom questions are `cards[<uuid>][field<N>]`; the question text is on `li.application-question`,
  NOT the nearer `<ul>` that `closest()` will find first
- The location input **clears itself on blur** unless a geo suggestion was chosen, and no
  suggestions are returned headless -> route to a human rather than reporting it filled
- Uses hCaptcha

**Workday**
- Public search endpoint: `POST /wday/cxs/<tenant>/<site>/jobs`
- Tenant, shard (`wd1`/`wd5`/`wd12`/`wd108`...) and site name ALL vary and are not guessable.
  Verify each against the live endpoint; the tenant root returns 406 for any hostname, so it
  cannot be used to confirm a tenant exists.
- The search endpoint returns **no description** - fetch each job's detail endpoint, or every
  job scores on its title alone
- Submitting needs an account per tenant, so discovery only

## Rules that keep being re-learned

1. **First-option-wins is almost always wrong.** It has produced: Afghanistan as the phone
   country, "East Boston" for Boston, and a click landing on a yes/no toggle instead of a
   location suggestion. Match the option; don't take the first.
2. **Read the rendered page, not your own bookkeeping.** `self.filled` only knows about widgets
   the scanner could see. `form.checkValidity()` and a screenshot are authoritative.
3. **A safety gate must fail closed.** A gate that returns "nothing wrong" when it errors is
   worse than no gate.
4. **Never treat a proxy as proof of submission.** Page text, a disabled button, and any POST
   that merely looks related have each produced false "submitted" reports. Require a 2xx POST to
   a real submission endpoint, or a reply from the company.
