"""Maps an application form's question labels to answers from profile.yaml.

Custom question IDs are per-job on every ATS (Greenhouse `question_19304433004`, Ashby UUIDs,
Lever `cards[uuid][field0]`), so matching has to go by the human-readable label text. Each
known question type carries a list of phrasings; we normalize the label and fuzzy-match.

Anything unmatched and required is deliberately NOT guessed - it goes back to the caller as
`unanswered`, which routes to a Claude draft (short free-text only) or to needs_human.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from rapidfuzz import fuzz

MATCH_THRESHOLD = 82

# question_key -> phrasings seen on real forms
QUESTION_PATTERNS: dict[str, list[str]] = {
    "first_name": ["first name", "given name", "preferred first name"],
    "last_name": ["last name", "surname", "family name"],
    "full_name": ["full name", "legal name", "your name", "name"],
    "email": ["email", "email address", "e-mail"],
    "phone": ["phone", "phone number", "mobile", "telephone"],
    # "how did you hear about this opportunity" was matching `location` at 85% on the word
    # "opportunity"/"about" overlap, which would have typed a city into a referral-source field.
    # Location phrasings are kept tight and anchored for that reason.
    "location": ["location", "current location", "location city", "city of residence",
                 "where are you located", "where do you currently live",
                 "from where do you intend to work", "current city"],
    "linkedin": ["linkedin profile", "linkedin url", "linkedin"],
    "portfolio": ["portfolio url", "website or portfolio", "portfolio", "personal website", "website",
                  "portfolio + password", "portfolio link"],
    "github": ["github url", "github profile", "github"],
    "current_company": ["current company", "current employer", "company"],
    "how_did_you_hear": ["how did you hear about us", "how did you find", "referral source",
                         "how did you hear about this", "how did you hear about this opportunity",
                         "how did you learn about", "where did you hear about"],
    "work_authorization": ["are you authorized to work", "authorized to work", "work authorization",
                           "legally authorized", "eligible to work",
                           "do you have a legal right to work", "legal right to work",
                           "are you legally eligible", "authorized to be employed"],
    "needs_sponsorship": ["require sponsorship", "need sponsorship", "will you now or in the future require",
                          "visa sponsorship", "require immigration", "sponsorship for employment visa",
                          "will you require sponsorship", "need visa support"],
    "previously_worked_here": ["have you ever worked for", "previously worked", "former employee",
                               "worked here before"],
    "related_to_employees": ["related to", "family member", "relatives employed"],
    "over_18": ["are you over 18", "at least 18", "18 years"],
    "years_experience": ["years of experience", "how many years"],
    "highest_degree": ["highest degree", "highest level of education", "education level"],
    "school": ["school", "university", "college", "institution"],
    "graduation_year": ["graduation year", "year of graduation", "expected graduation"],
    "desired_salary": ["salary expectation", "desired salary", "compensation expectation",
                       "expected salary", "salary requirement", "desired base compensation",
                       "desired compensation", "base compensation for this role",
                       "compensation requirement", "target compensation"],
    "start_date": ["start date", "when can you start", "earliest start", "available to start",
                   "notice period"],
    "pronouns": ["pronouns", "preferred pronouns"],
    "willing_to_relocate": ["willing to relocate", "open to relocation", "relocate"],
    "cover_letter": ["cover letter"],
    "additional_info": ["additional information", "anything else", "other information"],
}

EEO_PATTERNS: dict[str, list[str]] = {
    "gender": ["gender", "gender identity"],
    "race_ethnicity": ["race", "ethnicity", "racial", "ethnic background"],
    "hispanic_latino": ["hispanic", "latino", "latinx"],
    "veteran_status": ["veteran", "protected veteran", "military service"],
    "disability_status": ["disability", "disabled", "disability status"],
    "sexual_orientation": ["sexual orientation"],
    "transgender": ["transgender", "gender identity expression"],
}

# Text that means "I'd rather not say" across the option lists we've seen.
DECLINE_OPTION_HINTS = [
    "decline to self identify", "decline to self-identify", "i don't wish to answer",
    "i do not wish to answer", "prefer not to say", "prefer not to disclose",
    "choose not to disclose", "decline", "not to answer", "do not wish",
]

YES_HINTS = ["yes", "true"]
NO_HINTS = ["no", "false"]


def normalize_label(label: str) -> str:
    """Strips required markers, punctuation and whitespace noise from a form label."""
    s = (label or "").lower()
    s = s.replace("✱", " ").replace("*", " ")
    s = re.sub(r"\(required\)|\(optional\)", " ", s)
    s = re.sub(r"[^a-z0-9+/ ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Labels that superficially match a known key but mean something else entirely. Caught in
# practice: "Name Pronunciation | How do you pronounce your name" matched the name patterns and
# got filled with the full legal name.
NEGATIVE_LABEL_HINTS = [
    "pronounce", "pronunciation", "maiden", "nickname of", "company name", "school name",
    "manager name", "reference name", "emergency contact",
]


def classify_label(label: str) -> tuple[str | None, bool, int]:
    """Returns (question_key, is_eeo, score). question_key is None when nothing matched."""
    norm = normalize_label(label)
    if not norm:
        return None, False, 0
    if any(hint in norm for hint in NEGATIVE_LABEL_HINTS):
        return None, False, 0

    best_key, best_score, best_eeo = None, 0, False
    for key, phrasings in EEO_PATTERNS.items():
        for phrase in phrasings:
            score = max(fuzz.partial_ratio(norm, phrase), fuzz.token_set_ratio(norm, phrase))
            if score > best_score:
                best_key, best_score, best_eeo = key, score, True
    for key, phrasings in QUESTION_PATTERNS.items():
        for phrase in phrasings:
            score = max(fuzz.partial_ratio(norm, phrase), fuzz.token_set_ratio(norm, phrase))
            # exact-ish containment should win over a loose partial match on a short phrase
            if norm == phrase:
                score = 100
            if score > best_score:
                best_key, best_score, best_eeo = key, score, False

    if best_score < MATCH_THRESHOLD:
        return None, False, best_score
    return best_key, best_eeo, best_score


def is_required(label: str, required_attr: bool = False) -> bool:
    """Greenhouse marks required with '*' in the label rather than the required attribute;
    Ashby/Lever set the attribute (Lever also uses '✱')."""
    return bool(required_attr) or "*" in (label or "") or "✱" in (label or "")


@dataclass
class AnswerBook:
    """Flattened profile answers keyed by question_key."""
    values: dict[str, str]
    eeo: dict[str, str]

    def get(self, key: str) -> str | None:
        return self.values.get(key)


def build_answer_book(profile: dict[str, Any]) -> AnswerBook:
    name = profile.get("name", {}) or {}
    loc = profile.get("location", {}) or {}
    links = profile.get("links", {}) or {}
    auth = profile.get("work_authorization", {}) or {}
    prefs = profile.get("preferences", {}) or {}
    std = profile.get("standard_answers", {}) or {}

    city_state = ", ".join(x for x in (loc.get("city"), loc.get("state")) if x and x != "TODO")

    def tri_state(value: Any, yes: str = "Yes", no: str = "No") -> str:
        """Work-authorization answers are tri-state on purpose. An unset (null) value must NOT
        collapse to "No" - answering that on a real application is a misrepresentation. Unset
        returns "" so the field is left blank and routed to needs_human instead."""
        if value is None:
            return ""
        return yes if value else no

    needs_sponsorship = auth.get("needs_sponsorship_now"), auth.get("needs_sponsorship_future")
    if all(v is None for v in needs_sponsorship):
        sponsorship_answer = ""
    else:
        sponsorship_answer = "Yes" if any(bool(v) for v in needs_sponsorship) else "No"

    values = {
        "first_name": name.get("first", ""),
        "last_name": name.get("last", ""),
        "full_name": name.get("full", ""),
        "email": profile.get("email", ""),
        "phone": profile.get("phone", ""),
        "location": city_state,
        "linkedin": links.get("linkedin", ""),
        "portfolio": links.get("portfolio", ""),
        "github": links.get("github", ""),
        "pronouns": profile.get("pronouns", ""),
        "how_did_you_hear": std.get("how_did_you_hear", ""),
        "work_authorization": tri_state(auth.get("authorized_in_us")),
        "needs_sponsorship": sponsorship_answer,
        "previously_worked_here": std.get("previously_worked_here", "No"),
        "related_to_employees": std.get("related_to_employees", "No"),
        "over_18": std.get("over_18", "Yes"),
        "years_experience": str(std.get("years_experience_total", "")),
        "highest_degree": std.get("highest_degree", ""),
        "school": std.get("school", ""),
        "graduation_year": str(std.get("graduation_year", "")),
        "desired_salary": str(prefs.get("desired_salary_target", "")),
        "start_date": prefs.get("earliest_start_date", ""),
        "willing_to_relocate": "Yes" if prefs.get("willing_to_relocate") else "No",
        "current_company": std.get("current_company", ""),
    }
    values = {k: ("" if v in (None, "TODO", "None") else str(v)) for k, v in values.items()}
    eeo = {k: str(v) for k, v in (profile.get("eeo") or {}).items()}
    return AnswerBook(values=values, eeo=eeo)


# Fields where a merely-plausible fuzzy match is a factual misrepresentation, not a convenience.
# Caught in practice: "Indiana University Indianapolis" fuzzy-matched a dropdown's "University of
# Indianapolis" (a different school) at ~78%. These require a near-exact match or no answer.
STRICT_MATCH_KEYS = {"school", "highest_degree", "graduation_year", "work_authorization",
                     "needs_sponsorship", "years_experience", "current_company"}
STRICT_THRESHOLD = 93
DEFAULT_OPTION_THRESHOLD = 85


US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "georgia", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky",
    "louisiana", "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "new york", "north carolina", "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania",
    "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
}
STATE_ABBR = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california",
    "CO": "colorado", "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia",
    "HI": "hawaii", "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland", "MA": "massachusetts",
    "MI": "michigan", "MN": "minnesota", "MS": "mississippi", "MO": "missouri", "MT": "montana",
    "NE": "nebraska", "NV": "nevada", "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico",
    "NY": "new york", "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont",
    "VA": "virginia", "WA": "washington", "WV": "west virginia", "WI": "wisconsin", "WY": "wyoming",
}


def derive_answer(label: str, profile: dict[str, Any]) -> str | None:
    """Answers questions that profile.yaml implies but doesn't state literally.

    The motivating real case: "Do you live in one of the following states? Alabama, Alaska,
    Delaware, ..." - answerable from the profile's state without asking the user anything.
    """
    norm = normalize_label(label)
    loc = profile.get("location") or {}
    state_abbr = (loc.get("state") or "").upper()
    state_name = STATE_ABBR.get(state_abbr, "")

    if "do you live in one of the following" in norm or "reside in any of the following" in norm:
        listed = {s for s in US_STATE_NAMES if s in norm}
        if listed and state_name:
            return "Yes" if state_name in listed else "No"

    if state_name and ("do you live in" in norm or "are you located in" in norm or "reside in" in norm):
        for other in US_STATE_NAMES:
            if other in norm:
                return "Yes" if other == state_name else "No"

    if "are you based in the us" in norm or "located in the united states" in norm \
            or "authorized to work in the united states" in norm:
        country = (loc.get("country") or "").lower()
        if "united states" in country:
            return "Yes"
    return None


def pick_option(options: list[str], desired: str, eeo: bool = False, strict: bool = False) -> str | None:
    """Chooses the option text best matching `desired`. EEO fields fall back to a decline option.
    `strict` raises the bar for fields where a wrong-but-similar answer would be a lie."""
    if not options:
        return None
    cleaned = [(o, normalize_label(o)) for o in options if o and o.strip()]
    if eeo and (not desired or desired.lower() == "decline"):
        for original, norm in cleaned:
            if any(h in norm for h in DECLINE_OPTION_HINTS):
                return original
        return None

    if not desired:
        return None
    want = normalize_label(desired)

    if want in YES_HINTS or want in NO_HINTS:
        for original, norm in cleaned:
            if norm == want or norm.startswith(want + " "):
                return original

    best, best_score = None, 0
    for original, norm in cleaned:
        # token_sort keeps word-order differences from inflating the score the way
        # partial_ratio does ("University of Indianapolis" vs "Indiana University Indianapolis")
        score = max(fuzz.ratio(norm, want), fuzz.token_sort_ratio(norm, want))
        if score > best_score:
            best, best_score = original, score
    threshold = STRICT_THRESHOLD if strict else DEFAULT_OPTION_THRESHOLD
    return best if best_score >= threshold else None
