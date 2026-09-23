"""Ashby adapter. Structure verified against a live Ramp posting (2026-09-22):
  - #_systemfield_name is a SINGLE "Legal Name" field (not first/last like Greenhouse)
  - #_systemfield_email, #_systemfield_resume (file)
  - custom fields are named by UUID (or question_<id>), so match by label text
  - the `required` attribute IS reliable here, unlike Greenhouse
  - radio groups share a `name`; essay questions are plain textareas
"""
from __future__ import annotations

from .. import log
from .answers import is_required, pick_option
from .base import BaseApplyAdapter

logger = log.get("apply.ashby")


class AshbyAdapter(BaseApplyAdapter):
    ats = "ashby"

    def fill(self) -> None:
        full_name = self.answers.get("full_name") or " ".join(
            x for x in (self.answers.get("first_name"), self.answers.get("last_name")) if x
        )
        self.fill_if_present("#_systemfield_name", full_name, "full_name")
        self.fill_if_present("#_systemfield_email", self.answers.get("email") or "", "email")
        self.upload_resume("#_systemfield_resume")
        self._fill_location()
        self._fill_labeled_fields()
        self._fill_yesno_buttons()
        self._fill_consent_radios()

    def _field_blocks(self) -> list[dict]:
        return self.page.evaluate("""() => {
          const out = [];
          document.querySelectorAll('input, textarea, select').forEach(el => {
            if (el.type === 'hidden' || el.type === 'file') return;
            if (el.id && el.id.startsWith('_systemfield_')) return;
            let label = '';
            if (el.labels && el.labels[0]) label = el.labels[0].innerText;
            if (!label) label = el.getAttribute('aria-label') || '';
            if (!label) {
              const wrap = el.closest('div, fieldset');
              if (wrap) label = (wrap.innerText || '').split('\\n')[0];
            }
            out.push({name: el.name || '', id: el.id || '', tag: el.tagName,
                      type: el.type || '', required: el.required || false,
                      label: (label || '').trim().slice(0, 160)});
          });
          return out;
        }""")

    def _selector_for(self, block: dict) -> str | None:
        """Ashby field ids are UUIDs, which frequently start with a digit - invalid in a bare
        `#id` selector. Attribute selectors sidestep CSS escaping entirely."""
        if block.get("name"):
            return f'[name="{block["name"]}"]'
        if block.get("id"):
            return f'[id="{block["id"]}"]'
        return None

    def _fill_labeled_fields(self) -> None:
        handled_radio_groups: set[str] = set()
        for block in self._field_blocks():
            label = block["label"]
            if not label:
                continue
            required = is_required(label, block.get("required", False))
            answer, eeo = self.answer_for_label(label)

            if block["type"] in ("radio", "checkbox"):
                group = block.get("name") or ""
                if group in handled_radio_groups:
                    continue
                if answer or eeo:
                    if self._pick_radio(group, answer or "decline", eeo):
                        handled_radio_groups.add(group)
                        continue
                if required:
                    self.record_unanswered(label, True)
                continue

            if not answer:
                answer = self.maybe_draft(label, block["tag"] == "TEXTAREA")
            if not answer:
                if required:
                    self.record_unanswered(label, True,
                                           kind="textarea" if block["tag"] == "TEXTAREA" else "text")
                continue

            selector = self._selector_for(block)
            if not selector:
                continue
            if block["tag"] == "SELECT":
                self._select_option(selector, answer, label, eeo)
            else:
                self.fill_if_present(selector, answer, label[:50])

    def _pick_radio(self, group_name: str, desired: str, eeo: bool) -> bool:
        try:
            radios = self.page.locator(f'input[name="{group_name}"]')
            texts = []
            for i in range(radios.count()):
                el = radios.nth(i)
                lab = el.evaluate("e => (e.labels && e.labels[0]) ? e.labels[0].innerText : ''")
                texts.append((lab or "").strip())
            choice = pick_option(texts, desired, eeo=eeo)
            if choice is None:
                return False
            radios.nth(texts.index(choice)).check()
            self.filled[group_name[:40]] = choice
            return True
        except Exception as e:  # noqa: BLE001
            logger.debug("radio group %s failed: %s", group_name, e)
            return False

    def _select_option(self, selector: str, desired: str, label: str, eeo: bool) -> None:
        try:
            el = self.page.locator(selector).first
            texts = el.evaluate("e => Array.from(e.options).map(o => o.text)")
            choice = pick_option(texts, desired, eeo=eeo)
            if choice is None:
                self.record_unanswered(label, is_required(label), options=texts, kind="select")
                return
            el.select_option(label=choice)
            self.filled[label[:50]] = choice
        except Exception as e:  # noqa: BLE001
            logger.debug("select %s failed: %s", label[:40], e)
            self.record_unanswered(label, is_required(label))

    def _fill_location(self) -> None:
        """Ashby's Location is an input[role=combobox] with no id or name, backed by a remote
        suggestion list. Typing alone leaves it unset.

        Selection is done with ArrowDown+Enter rather than clicking an option: a
        `[class*=option]` click target also matches the yes/no toggle buttons and radio labels
        on the same page, so the click landed on the wrong element. Verified on a live Patreon
        posting - keyboard selection resolves "Boston" to "Boston, Massachusetts, United States".
        """
        value = self.answers.get("location") or ""
        if not value:
            return
        try:
            box = self.page.locator('.ashby-application-form-field-entry input[role="combobox"]').first
            if box.count() == 0 or not box.is_visible():
                return
            box.click()
            box.fill("")
            box.type(value.split(",")[0], delay=120)   # city alone matches the suggestion list better
            self.page.wait_for_timeout(2200)
            self.page.keyboard.press("ArrowDown")
            self.page.wait_for_timeout(250)
            self.page.keyboard.press("Enter")
            self.page.wait_for_timeout(500)

            resolved = (box.input_value() or "").strip()
            if resolved:
                self.filled["location"] = resolved
                self.drop_unanswered("Location")
            else:
                self.record_unanswered("Location", True, kind="combobox")
        except Exception as e:  # noqa: BLE001
            logger.debug("ashby location failed: %s", e)
            self.record_unanswered("Location", True, kind="combobox")

    def _fill_consent_radios(self) -> None:
        """The SMS-consent radio pair sits inside the Phone field entry, so an unanswered
        consent makes the whole Phone group read as empty at submit time. Declining is both a
        valid answer and the privacy-preserving default."""
        try:
            radios = self.page.locator('input[name="communicationConsent"]')
            if radios.count() == 0:
                return
            if radios.evaluate_all("els => els.some(e => e.checked)"):
                return
            texts = []
            for i in range(radios.count()):
                lab = radios.nth(i).evaluate("e => (e.labels && e.labels[0]) ? e.labels[0].innerText : ''")
                texts.append((lab or "").strip())
            desired = str((self.profile.get("standard_answers") or {}).get("sms_consent", "No"))
            choice = pick_option(texts, desired)
            if choice is None:
                return
            radios.nth(texts.index(choice)).check()
            self.filled["sms_consent"] = choice
        except Exception as e:  # noqa: BLE001
            logger.debug("consent radios failed: %s", e)

    def _yesno_blocks(self) -> list[dict]:
        """Ashby renders Yes/No questions as <button data-option> pairs, not inputs.

        This matters more than it sounds: a querySelectorAll over input/textarea/select never
        sees them, so they were neither filled NOR recorded as unanswered - a required question
        that is simply invisible. On a live Eagle posting three required questions (sponsorship,
        travel, relocation) were all blank while the run reported unanswered=0.
        """
        return self.page.evaluate("""() => {
          const out = [];
          document.querySelectorAll('.ashby-application-form-field-entry').forEach(entry => {
            const opts = entry.querySelectorAll('button[data-option]');
            if (!opts.length) return;
            const label = (entry.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean)[0] || '';
            const answered = Array.from(opts).some(o => o.getAttribute('aria-pressed') === 'true');
            out.push({
              label: label.slice(0, 200),
              answered,
              options: Array.from(opts).map(o => (o.getAttribute('data-option') || '').trim()),
            });
          });
          return out;
        }""")

    def _fill_yesno_buttons(self) -> None:
        for block in self._yesno_blocks():
            label = block["label"]
            if block["answered"] or not label:
                continue
            required = is_required(label, True)  # Ashby marks these required via the * in label
            answer, eeo = self.answer_for_label(label)
            if not answer:
                if required:
                    self.record_unanswered(label, True, options=block["options"], kind="yesno")
                continue

            choice = pick_option(block["options"], answer, eeo=eeo,
                                 strict=self.is_strict_label(label))
            if choice is None:
                if required:
                    self.record_unanswered(label, True, options=block["options"], kind="yesno")
                continue
            if self._click_yesno(label, choice):
                self.filled[label[:50]] = choice
                self.drop_unanswered(label)
            elif required:
                self.record_unanswered(label, True, options=block["options"], kind="yesno")

    def _click_yesno(self, label: str, option: str) -> bool:
        """Clicks the option button inside the entry whose text starts with `label`, then
        verifies aria-pressed actually flipped - a click that silently did nothing would
        otherwise be reported as filled."""
        try:
            entries = self.page.locator(".ashby-application-form-field-entry")
            for i in range(entries.count()):
                entry = entries.nth(i)
                text = (entry.inner_text() or "").strip()
                if not text.startswith(label[:40]):
                    continue
                btn = entry.locator(f'button[data-option="{option}"]').first
                if btn.count() == 0:
                    return False
                btn.click()
                self.page.wait_for_timeout(250)
                return btn.get_attribute("aria-pressed") == "true"
        except Exception as e:  # noqa: BLE001
            logger.debug("yes/no click failed for %s: %s", label[:40], e)
        return False

    def submit(self) -> bool:
        return self.click_submit('button:has-text("Submit Application"), button[type="submit"]')
