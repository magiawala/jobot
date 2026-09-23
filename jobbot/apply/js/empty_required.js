// Finds required form fields that are still empty. Runs immediately before an irreversible
// submit, because the adapter's own `unanswered` list is only as good as its scanner - a widget
// the scanner cannot see (Ashby's <button> yes/no toggles) is invisible to it.
//
// Required-ness is detected four ways because no single one holds across ATSs:
//   1. required / aria-required attributes          (Ashby inputs)
//   2. '*' or the Lever glyph in visible text        (Greenhouse, Lever)
//   3. a `_required` class on the label              (Ashby)
//   4. a CSS ::after asterisk                        (Ashby renders content:"*", which
//      innerText does NOT include - a text-only check silently passed every blank Ashby field)
() => {
  const GROUP_SEL = [
    '.ashby-application-form-field-entry',
    'li.application-question',
    '.application-field',
    'fieldset',
    '[class*="field-entry"]'
  ].join(', ');

  const STAR = /[*✚]/;
  const missing = [];
  const seen = new Set();

  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };

  const starAfter = (el) => {
    try {
      const c = window.getComputedStyle(el, '::after').content || '';
      return STAR.test(c);
    } catch (_) {
      return false;
    }
  };

  const groupRequired = (g) => {
    if (STAR.test(g.innerText || '')) return true;
    const labels = g.querySelectorAll('label, [class*="label"], [class*="heading"]');
    for (const l of labels) {
      const cls = (l.className || '').toString();
      if (/_required|--required|\brequired\b/.test(cls)) return true;
      if (starAfter(l)) return true;
    }
    return !!g.querySelector('[required], [aria-required="true"]');
  };

  const groupLabel = (g) => {
    const l = g.querySelector('label, [class*="label"], [class*="heading"]');
    const t = l ? (l.innerText || '').trim() : '';
    const fallback = (g.innerText || '').split('\n').map((s) => s.trim()).filter(Boolean)[0] || '';
    return (t || fallback).slice(0, 110);
  };

  const groupAnswered = (g) => {
    const opts = g.querySelectorAll('button[data-option]');
    if (opts.length) return Array.from(opts).some((o) => o.getAttribute('aria-pressed') === 'true');

    const choices = g.querySelectorAll('input[type=radio], input[type=checkbox]');
    if (choices.length) return Array.from(choices).some((c) => c.checked);

    const files = g.querySelectorAll('input[type=file]');
    if (files.length) return Array.from(files).some((f) => f.files && f.files.length);

    const inputs = g.querySelectorAll('input:not([type=hidden]), select, textarea');
    if (inputs.length) return Array.from(inputs).some((i) => (i.value || '').trim());

    return true; // nothing fillable here - don't flag a prose block
  };

  document.querySelectorAll(GROUP_SEL).forEach((g) => {
    if (!isVisible(g)) return;
    if (g.querySelector(GROUP_SEL)) return; // leaf groups only, not section wrappers
    if (!groupRequired(g)) return;
    if (groupAnswered(g)) return;
    const label = groupLabel(g) || '(unlabelled required field)';
    if (seen.has(label)) return;
    seen.add(label);
    missing.push(label);
  });

  return missing.slice(0, 20);
}
