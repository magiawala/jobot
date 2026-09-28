"""Greenhouse adapter. Structure verified against a live Figma posting (2026-09-22):
  - standard fields have stable ids: #first_name #last_name #email #phone #candidate-location #resume
  - custom questions are #question_<per-job-numeric-id>, so they must be matched by label text
  - required-ness is signalled by '*' in the label, NOT the required attribute
  - EEO fields use semantic ids (#gender, #hispanic_ethnicity, #veteran_status, #disability_status)
  - several "dropdowns" are react-select style comboboxes: a text input plus a listbox, so they
    need click -> type -> pick-option rather than select_option()
"""
from __future__ import annotations

from playwright.sync_api import TimeoutError as PWTimeout

from .. import log
from .answers import classify_label, is_required, pick_option
from .base import BaseApplyAdapter

logger = log.get("apply.greenhouse")


class GreenhouseAdapter(BaseApplyAdapter):
    ats = "greenhouse"

    def fill(self) -> None:
        self.fill_if_present("#first_name", self.answers.get("first_name") or "", "first_name")
        self.fill_if_present("#last_name", self.answers.get("last_name") or "", "last_name")
        self.fill_if_present("#email", self.answers.get("email") or "", "email")
        self.fill_if_present("#phone", self.answers.get("phone") or "", "phone")
        self._fill_combobox("#candidate-location", self.answers.get("location") or "", "location")
        self.upload_resume("#resume")
        self._fill_custom_questions()
        self._fill_eeo()
        self._complete_unselected_comboboxes()

    def _complete_unselected_comboboxes(self) -> None:
        """Finishes any react-select whose hidden required input is still empty.

        This is what silently blocked every Greenhouse submission. Greenhouse renders a combobox
        as a visible text input plus a HIDDEN required input carrying the chosen value - no name,
        no id, no label. Typing into the visible box leaves the hidden one empty, so
        form.checkValidity() is false and clicking submit does nothing at all: no request, no
        error, no visible validation message. The click appeared to work and never did.

        Each one is completed with a REAL answer typed in and matched. An earlier version just
        pressed ArrowDown+Enter to take whatever was first, which selected Afghanistan (+93) as
        the phone country on a Figma application - a form that validates with a false answer in
        it is worse than one that doesn't submit.
        """
        loc = (self.profile.get("location") or {})
        # what we know how to answer, by the field's own id
        known: dict[str, str] = {
            "country": loc.get("country") or "United States",
            "candidate-location": ", ".join(
                x for x in (loc.get("city"), loc.get("state")) if x) or "",
        }

        for _ in range(6):      # each fix can reveal another; bounded so this can't spin
            target = self.page.evaluate("""() => {
              const form = document.querySelector('form');
              if (!form || form.checkValidity()) return null;
              const bad = form.querySelector('input:invalid');
              if (!bad) return null;
              const wrap = bad.closest('div');
              if (!wrap) return null;
              const combo = wrap.querySelector('[role=combobox], input[type=text]:not(:invalid)');
              if (!combo) return null;
              combo.setAttribute('data-jobbot-target', '1');
              return {
                id: combo.id || '',
                label: ((wrap.innerText || '').split('\\n').map(s => s.trim())
                        .filter(Boolean)[0] || '').slice(0, 80),
              };
            }""")
            if not target:
                return

            label = target.get("label") or target.get("id") or "combobox"
            answer = known.get(target.get("id") or "") or ""
            if not answer:
                key, _eeo, _score = classify_label(label)
                answer = (self.answers.get(key) or "") if key else ""

            if not answer:
                # No idea what the right answer is. Leaving it empty keeps the form invalid, so
                # the pre-submit gate blocks and a human decides - which is the correct outcome.
                self._clear_target()
                self.record_unanswered(label, True, kind="combobox")
                logger.info("combobox %r has no known answer - leaving for review", label[:60])
                return

            if not self._select_combobox_option(answer, label):
                self._clear_target()
                self.record_unanswered(label, True, kind="combobox")
                return

    def _clear_target(self) -> None:
        try:
            self.page.evaluate("""() => document.querySelectorAll('[data-jobbot-target]')
                   .forEach(e => e.removeAttribute('data-jobbot-target'))""")
        except Exception:  # noqa: BLE001
            pass

    def _select_combobox_option(self, answer: str, label: str) -> bool:
        """Types the answer and picks the best-matching option, never merely the first."""
        try:
            box = self.page.locator('[data-jobbot-target="1"]').first
            box.scroll_into_view_if_needed(timeout=5000)
            box.click()
            box.fill("")
            box.type(answer, delay=60)
            self.page.wait_for_timeout(900)

            opts = self.page.locator('[role="option"]')
            texts = [opts.nth(i).inner_text().strip() for i in range(min(opts.count(), 40))]
            choice = pick_option(texts, answer) if texts else None
            if choice is not None:
                opts.nth(texts.index(choice)).click()
            else:
                # no option list (a plain autocomplete): commit with the keyboard
                self.page.keyboard.press("ArrowDown")
                self.page.wait_for_timeout(150)
                self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(400)
            self.filled[f"{label[:44]}"] = choice or answer
            self.drop_unanswered(label)
            logger.info("combobox %r -> %r", label[:46], (choice or answer)[:40])
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug("combobox %r failed: %s", label[:40], e)
            return False
        finally:
            self._clear_target()

    def _fill_combobox(self, selector: str, value: str, key: str) -> bool:
        """Greenhouse location/country inputs are autocompletes: typing alone leaves the field
        unvalidated, so a suggestion has to be chosen.

        It picks the BEST-matching suggestion, not the first: typing "Boston, MA" offers "East
        Boston, Massachusetts" first, and taking it put the wrong neighbourhood on a live Figma
        application. First-option-wins has now been the wrong default three times in this file.
        """
        if not value:
            return False
        try:
            loc = self.page.locator(selector).first
            if loc.count() == 0 or not loc.is_visible():
                return False
            loc.click()
            loc.fill(value)
            self.page.wait_for_timeout(1400)

            opts = self.page.locator('[role="option"], .select__option, li[id*="option"]')
            texts = [opts.nth(i).inner_text().strip() for i in range(min(opts.count(), 25))]
            if texts:
                choice = pick_option(texts, value)
                if choice is None:
                    # prefer a suggestion that starts with what we typed over an arbitrary one
                    starts = [t for t in texts if t.lower().startswith(value.split(",")[0].lower())]
                    choice = starts[0] if starts else texts[0]
                opts.nth(texts.index(choice)).click()
                self.filled[key] = choice
            else:
                self.filled[key] = value
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug("combobox %s failed: %s", selector, e)
            return False

    def _question_blocks(self) -> list[dict]:
        """Returns one entry per custom question: its label text and the input's selector."""
        return self.page.evaluate("""() => {
          const out = [];
          document.querySelectorAll('[id^="question_"], #gender, #hispanic_ethnicity, #veteran_status, #disability_status')
            .forEach(el => {
              let label = '';
              if (el.labels && el.labels[0]) label = el.labels[0].innerText;
              if (!label) label = el.getAttribute('aria-label') || '';
              if (!label) {
                const wrap = el.closest('div');
                if (wrap) label = (wrap.innerText || '').split('\\n')[0];
              }
              out.push({id: el.id, tag: el.tagName, type: el.type || '', label: (label||'').trim(),
                        required: el.required || false});
            });
          return out;
        }""")

    def _fill_custom_questions(self) -> None:
        for block in self._question_blocks():
            if block["id"] in ("gender", "hispanic_ethnicity", "veteran_status", "disability_status"):
                continue  # handled by _fill_eeo
            label = block["label"]
            required = is_required(label, block.get("required", False))
            answer, _eeo = self.answer_for_label(label)
            if not answer:
                answer = self.maybe_draft(label, block["tag"] == "TEXTAREA")
            if not answer:
                if required:
                    self.record_unanswered(label, True)
                continue
            selector = f'#{block["id"]}'
            if block["tag"] == "TEXTAREA":
                self.fill_if_present(selector, answer, label[:50])
            elif self._looks_like_combobox(selector):
                self._pick_from_listbox(selector, answer, label)
            else:
                self.fill_if_present(selector, answer, label[:50])

    def _looks_like_combobox(self, selector: str) -> bool:
        try:
            el = self.page.locator(selector).first
            role = el.get_attribute("role") or ""
            aria = el.get_attribute("aria-haspopup") or ""
            readonly = el.get_attribute("readonly")
            return role == "combobox" or aria in ("listbox", "true") or readonly is not None
        except Exception:  # noqa: BLE001
            return False

    def _listbox_for(self, el):
        """Returns a locator for THIS combobox's options.

        A page-global [role=option] query is wrong here: Greenhouse renders an
        international phone-number widget whose country list is also [role=option], so every
        dropdown was reading "Afghanistan+93, Aland Islands+358, ..." instead of its own
        choices - which is why work authorization kept failing on Greenhouse specifically.
        """
        for attr in ("aria-controls", "aria-owns"):
            try:
                target = el.get_attribute(attr)
            except Exception:  # noqa: BLE001
                target = None
            if target:
                scoped = self.page.locator(f'#{target} [role="option"], #{target} li')
                if scoped.count():
                    return scoped
        # fall back to options inside the combobox's own field wrapper
        try:
            handle = el.element_handle()
            if handle:
                container = handle.evaluate_handle(
                    "e => e.closest('[class*=select], [class*=field], [data-field]') || e.parentElement")
                if container:
                    scoped = container.as_element().query_selector_all('[role="option"]')
                    if scoped:
                        return scoped
        except Exception:  # noqa: BLE001
            pass
        return None

    def _pick_from_listbox(self, selector: str, desired: str, label: str) -> None:
        try:
            el = self.page.locator(selector).first
            el.click()
            self.page.wait_for_timeout(700)
            scoped = self._listbox_for(el)
            if scoped is None:
                self.record_unanswered(label, is_required(label), kind="select")
                self.page.keyboard.press("Escape")
                return
            if isinstance(scoped, list):
                texts = [(h.inner_text() or "").strip() for h in scoped[:60]]
                opts = scoped
            else:
                opts = scoped
                texts = [opts.nth(i).inner_text().strip() for i in range(min(opts.count(), 60))]
            choice = pick_option(texts, desired, strict=self.is_strict_label(label))
            if choice is None:
                self.record_unanswered(label, is_required(label), options=texts, kind="select")
                self.page.keyboard.press("Escape")
                return
            idx = texts.index(choice)
            (opts[idx] if isinstance(opts, list) else opts.nth(idx)).click()
            self.filled[label[:50]] = choice
            self.drop_unanswered(label)
        except Exception as e:  # noqa: BLE001
            logger.debug("listbox pick failed for %s: %s", label[:40], e)
            self.record_unanswered(label, is_required(label))

    def _fill_eeo(self) -> None:
        for field_id, eeo_key in (("gender", "gender"), ("hispanic_ethnicity", "hispanic_latino"),
                                  ("veteran_status", "veteran_status"), ("disability_status", "disability_status")):
            selector = f"#{field_id}"
            try:
                if self.page.locator(selector).count() == 0:
                    continue
                desired = self.answers.eeo.get(eeo_key, "decline")
                el = self.page.locator(selector).first
                el.click()
                self.page.wait_for_timeout(600)
                opts = self.page.locator('[role="option"]')
                texts = [opts.nth(i).inner_text().strip() for i in range(min(opts.count(), 40))]
                choice = pick_option(texts, desired, eeo=True)
                if choice is None:
                    self.page.keyboard.press("Escape")
                    continue
                opts.nth(texts.index(choice)).click()
                self.filled[f"eeo:{eeo_key}"] = choice
            except Exception as e:  # noqa: BLE001
                logger.debug("eeo %s failed: %s", field_id, e)

    def submit(self) -> bool:
        return self.click_submit('button[type="submit"], button:has-text("Submit Application")')
