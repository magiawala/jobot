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
from .answers import is_required, pick_option
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

    def _fill_combobox(self, selector: str, value: str, key: str) -> bool:
        """Greenhouse location/country inputs are autocompletes: typing alone leaves the field
        unvalidated, so we pick the first suggestion."""
        if not value:
            return False
        try:
            loc = self.page.locator(selector).first
            if loc.count() == 0 or not loc.is_visible():
                return False
            loc.click()
            loc.fill(value)
            self.page.wait_for_timeout(1200)
            option = self.page.locator('[role="option"], .select__option, li[id*="option"]').first
            if option.count() > 0 and option.is_visible():
                option.click()
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
