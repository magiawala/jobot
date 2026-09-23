"""Lever adapter. Structure verified against a live Palantir posting (2026-09-22):
  - semantic field names: name (single full name), email, phone, location, org
  - links are conveniently named: urls[LinkedIn], urls[GitHub], urls[Portfolio]
  - custom questions live under cards[<uuid>][field<N>] as text/radio/checkbox/select/textarea,
    with the question text in the surrounding .application-question block
  - EEO uses eeo[veteran] / eeo[disability] selects
  - uses hCaptcha (not reCAPTCHA); base.check_captcha covers both
  - required is marked both by the attribute and a '✱' glyph in the label
"""
from __future__ import annotations

from .. import log
from .answers import is_required, pick_option
from .base import BaseApplyAdapter

logger = log.get("apply.lever")


class LeverAdapter(BaseApplyAdapter):
    ats = "lever"

    def fill(self) -> None:
        full_name = self.answers.get("full_name") or " ".join(
            x for x in (self.answers.get("first_name"), self.answers.get("last_name")) if x
        )
        self.fill_if_present('input[name="name"]', full_name, "full_name")
        self.fill_if_present('input[name="email"]', self.answers.get("email") or "", "email")
        self.fill_if_present('input[name="phone"]', self.answers.get("phone") or "", "phone")
        self._fill_location(self.answers.get("location") or "")
        self.fill_if_present('input[name="org"]', self.answers.get("current_company") or "", "current_company")
        self.fill_if_present('input[name="urls[LinkedIn]"]', self.answers.get("linkedin") or "", "linkedin")
        self.fill_if_present('input[name="urls[GitHub]"]', self.answers.get("github") or "", "github")
        self.fill_if_present('input[name="urls[Portfolio]"]', self.answers.get("portfolio") or "", "portfolio")
        self.upload_resume('input[type="file"][name="resume"], #resume-upload-input')
        self._fill_cards()
        self._fill_eeo()

    def _fill_location(self, value: str) -> None:
        """Lever's location is an autocomplete backed by a hidden selectedLocation field; picking
        a suggestion is what actually sets it."""
        if not value:
            return
        try:
            el = self.page.locator("#location-input, input[name='location']").first
            if el.count() == 0:
                return
            el.click()
            el.fill(value)
            self.page.wait_for_timeout(1200)
            suggestion = self.page.locator(
                ".dropdown-location .dropdown-location-option, [role='option'], .location-dropdown li,"
                " .dropdown-locations li, ul.dropdown-locations > li"
            ).first
            if suggestion.count() > 0 and suggestion.is_visible():
                suggestion.click()
                self.page.wait_for_timeout(400)

            # Lever's location input is backed by a geo-autocomplete and CLEARS ITSELF ON BLUR
            # unless a suggestion was actually chosen (verified against a live posting: typing
            # real keystrokes produced no suggestions and the value was empty after blur). So we
            # must blur first and re-read - checking immediately would report a false success and
            # let AUTO mode submit with a required field empty.
            self.page.locator("input[name='email']").first.click()
            self.page.wait_for_timeout(500)
            typed = (el.input_value() or "").strip()
            if typed:
                self.filled["location"] = typed
            else:
                logger.info("lever location did not persist (no autocomplete suggestion) - needs human")
                self.record_unanswered("Current location (Lever autocomplete did not resolve)", True)
        except Exception as e:  # noqa: BLE001
            logger.debug("location autocomplete failed: %s", e)
            self.record_unanswered("Current location", True)

    def _card_blocks(self) -> list[dict]:
        return self.page.evaluate("""() => {
          const out = [];
          document.querySelectorAll('[name^="cards["]').forEach(el => {
            if (el.type === 'hidden') return;
            // The question text lives on li.application-question. A radio/checkbox's own <label>
            // (and its wrapping ul/li) is the OPTION text ("Yes", "English (ENG)"), which is
            // useless for classifying the question - so query the question wrapper SPECIFICALLY
            // rather than via a combined closest(), which would match the nearer <ul> first.
            const card = el.closest('.application-question') || el.closest('.card-field, fieldset');
            let label = '';
            if (card) {
              const t = card.querySelector('.application-label, .text, .application-question-label');
              label = t ? t.innerText : '';
              if (!label) {
                label = (card.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean)[0] || '';
              }
            }
            if (!label && el.labels && el.labels[0] && el.type !== 'radio' && el.type !== 'checkbox') {
              label = el.labels[0].innerText;
            }
            let optLabel = '';
            if (el.labels && el.labels[0]) optLabel = el.labels[0].innerText;
            out.push({name: el.name, tag: el.tagName, type: el.type || '',
                      required: el.required || false,
                      label: (label || '').trim().slice(0, 200),
                      optionLabel: (optLabel || '').trim().slice(0, 80)});
          });
          return out;
        }""")

    def _fill_cards(self) -> None:
        groups: dict[str, list[dict]] = {}
        for block in self._card_blocks():
            groups.setdefault(block["name"], []).append(block)

        for name, blocks in groups.items():
            first = blocks[0]
            label = first["label"]
            required = is_required(label, first.get("required", False))
            answer, eeo = self.answer_for_label(label)

            if first["type"] in ("radio", "checkbox"):
                texts = [b["optionLabel"] for b in blocks]
                choice = pick_option(texts, answer or "decline", eeo=eeo,
                                     strict=self.is_strict_label(label)) if (answer or eeo) else None
                if choice is None:
                    if required:
                        self.record_unanswered(label, True)
                    continue
                try:
                    self.page.locator(f'[name="{name}"]').nth(texts.index(choice)).check()
                    self.filled[label[:50] or name[:40]] = choice
                except Exception as e:  # noqa: BLE001
                    logger.debug("card radio %s failed: %s", name, e)
                    if required:
                        self.record_unanswered(label, True)
                continue

            if not answer:
                answer = self.maybe_draft(label, first["tag"] == "TEXTAREA")
            if not answer:
                if required:
                    self.record_unanswered(label, True,
                                           kind="textarea" if first["tag"] == "TEXTAREA" else "text")
                continue

            selector = f'[name="{name}"]'
            if first["tag"] == "SELECT":
                try:
                    el = self.page.locator(selector).first
                    texts = el.evaluate("e => Array.from(e.options).map(o => o.text)")
                    choice = pick_option(texts, answer, eeo=eeo, strict=self.is_strict_label(label))
                    if choice is None:
                        if required:
                            self.record_unanswered(label, True)
                        continue
                    el.select_option(label=choice)
                    self.filled[label[:50]] = choice
                except Exception as e:  # noqa: BLE001
                    logger.debug("card select %s failed: %s", name, e)
                    if required:
                        self.record_unanswered(label, True)
            else:
                self.fill_if_present(selector, answer, label[:50] or name[:40])

    def _fill_eeo(self) -> None:
        for field_name, eeo_key in (("eeo[veteran]", "veteran_status"), ("eeo[disability]", "disability_status")):
            try:
                el = self.page.locator(f'select[name="{field_name}"]').first
                if el.count() == 0:
                    continue
                texts = el.evaluate("e => Array.from(e.options).map(o => o.text)")
                choice = pick_option(texts, self.answers.eeo.get(eeo_key, "decline"), eeo=True)
                if choice is None:
                    continue
                el.select_option(label=choice)
                self.filled[f"eeo:{eeo_key}"] = choice
            except Exception as e:  # noqa: BLE001
                logger.debug("lever eeo %s failed: %s", field_name, e)

    def submit(self) -> bool:
        return self.click_submit('#btn-submit, .postings-btn[type="submit"], button[type="submit"]')
