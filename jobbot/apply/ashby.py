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
        self._fill_labeled_fields()

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

    def submit(self) -> bool:
        btn = self.page.locator('button[type="submit"], button:has-text("Submit Application")').first
        btn.click()
        return True
