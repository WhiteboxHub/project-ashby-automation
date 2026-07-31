"""Bidirectional form sync helpers (terminal ↔ browser) and submission detection."""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from playwright.sync_api import Page

from jobcli.orchestration.human_interaction import humanized_fill
from jobcli.utils.fill_guard import is_meaningful_value, read_locator_value

CONFIRMATION_TEXTS = (
    "thank you",
    "thanks!",
    "thanks for applying",
    "application submitted",
    "successfully submitted",
    "application received",
    "application is received",
    "we have received your application",
    "we've received your application",
    "application sent",
    "application complete",
    "your application has been submitted",
    "you've applied",
    "you have applied",
    "we'll be in touch",
    "we will be in touch",
    "thank you for your interest",
    "thanks for your interest",
    "application confirmed",
)

CONFIRMATION_URL_TERMS = (
    "success",
    "confirmation",
    "confirm",
    "thank-you",
    "thank_you",
    "thanks",
    "submitted",
    "application-confirmation",
    "complete",
    "completed",
    "applied",
)

_SUBMIT_BTN_JS = r"""() => {
    const candidates = Array.from(
        document.querySelectorAll(
            "button[type='submit'], input[type='submit'], " +
            "button, [role='button']"
        )
    );
    const labelRe = /\b(submit|apply\s*now|send\s*application|submit\s*application)\b/i;
    for (const el of candidates) {
        try {
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            const s = getComputedStyle(el);
            if (s.display === 'none' || s.visibility === 'hidden') continue;
            const txt = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
            const typedSubmit =
                el.tagName === 'BUTTON' && el.getAttribute('type') === 'submit' ||
                el.tagName === 'INPUT'  && el.getAttribute('type') === 'submit';
            if (typedSubmit || labelRe.test(txt)) return true;
        } catch (_) {}
    }
    return false;
}"""

_VALIDATION_ERROR_JS = r"""(sel) => {
    const out = [];
    const seen = new Set();
    document.querySelectorAll(sel).forEach((el) => {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) return;
        const s = getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden') return;
        const t = (el.innerText || el.textContent || '').trim();
        if (!t || t.length > 200) return;
        const lower = t.toLowerCase();
        if (lower.includes('success') || lower.includes('completed') ||
            lower.includes('imported') || lower.includes('saved') ||
            lower.includes('optional')) return;
        if (seen.has(t)) return;
        seen.add(t);
        out.push(t);
    });
    return out;
}"""

_SNAPSHOT_JS = r"""() => {
    const res = {};
    const PLACEHOLDERS = ['select', 'choose', '--', 'please choose', 'select...'];
    document.querySelectorAll('input, select, textarea').forEach(el => {
        if (el.type === 'password' || el.type === 'hidden' || el.type === 'file') return;
        let val = el.value;
        if (el.tagName === 'SELECT' && el.selectedIndex >= 0) {
            val = el.options[el.selectedIndex].text;
        }
        if (!val || !String(val).trim()) return;
        const v = String(val).trim();
        const low = v.toLowerCase();
        if (PLACEHOLDERS.some(p => low === p || low.startsWith(p))) return;
        let label = (el.getAttribute('aria-label') || '').trim();
        if (!label && el.id) {
            const lbl = document.querySelector(`label[for="${el.id}"]`);
            if (lbl) label = (lbl.innerText || lbl.textContent || '').trim();
        }
        if (!label) label = (el.name || el.id || '').trim();
        if (!label || v.length > 200) return;
        res[label] = v;
    });
    return res;
}"""


def _submit_button_visible(page: Page) -> bool:
    if page is None:
        return False
    try:
        return bool(page.evaluate(_SUBMIT_BTN_JS))
    except Exception:
        return True


def _live_validation_errors(page: Page) -> list[str]:
    if page is None:
        return []
    selector = ",".join(
        [
            "[aria-invalid='true']",
            ".field-error",
            ".error-message",
            ".error-text",
            ".invalid-feedback",
            ".form-error",
            ".input-error",
            "[role='alert']",
        ]
    )
    try:
        msgs = page.evaluate(_VALIDATION_ERROR_JS, selector) or []
    except Exception:
        return []
    return [str(m) for m in msgs if m]


def looks_like_confirmation(
    page: Page,
    pre_submit_url: str,
    pre_submit_had_submit_btn: bool,
) -> tuple[bool, bool, dict[str, Any]]:
    """Return ``(strong, soft, signals)`` for post-submit confirmation heuristics."""
    try:
        page_text = (
            str(
                page.evaluate(
                    "() => (document.body ? document.body.innerText : '').toLowerCase()"
                )
                or ""
            ).lower()
        )[:20_000]
    except Exception:
        page_text = ""
    text_confirmed = any(t in page_text for t in CONFIRMATION_TEXTS)

    try:
        url_now = (page.url or "").lower()
    except Exception:
        url_now = ""
    url_confirmed = any(term in url_now for term in CONFIRMATION_URL_TERMS)
    url_changed = bool(
        pre_submit_url and url_now and url_now != pre_submit_url.lower()
    )

    submit_btn_still_there = _submit_button_visible(page)
    form_disappeared = bool(pre_submit_had_submit_btn and not submit_btn_still_there)
    has_errors = bool(_live_validation_errors(page))

    strong = bool(text_confirmed or url_confirmed)
    soft = bool((url_changed or form_disappeared) and not has_errors)

    return strong, soft, {
        "text_confirmed": text_confirmed,
        "url_confirmed": url_confirmed,
        "url_changed": url_changed,
        "form_disappeared": form_disappeared,
        "has_errors": has_errors,
    }


def snapshot_field_values(page: Page) -> dict[str, str]:
    """Lightweight map of visible form labels → current values."""
    if page is None:
        return {}
    try:
        raw = page.evaluate(_SNAPSHOT_JS) or {}
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for k, v in raw.items():
            if k and v is not None:
                sv = str(v).strip()
                if sv and is_meaningful_value(sv):
                    out[str(k).strip()] = sv
        return out
    except Exception:
        return {}


def _locators_for_label(page: Page, label: str) -> list:
    clean = (label or "").strip()
    if not clean:
        return []
    locs = []
    for factory in (
        lambda: page.get_by_label(clean, exact=False).first,
        lambda: page.get_by_placeholder(clean, exact=False).first,
        lambda: page.get_by_role("textbox", name=clean, exact=False).first,
        lambda: page.get_by_role("combobox", name=clean, exact=False).first,
    ):
        try:
            locs.append(factory())
        except Exception:
            pass
    return locs


def _try_select_option(page: Page, loc, value: str, options: Optional[list[str]]) -> bool:
    val = (value or "").strip()
    if not val:
        return False
    try:
        if loc.count() == 0:
            return False
        target = loc.first
        if not target.is_visible(timeout=1500):
            return False
        target.click(timeout=3000)
        page.wait_for_timeout(200)
        for pick in (val, *(options or [])):
            if not pick:
                continue
            try:
                page.get_by_role("option", name=pick, exact=False).first.click(timeout=2000)
                return True
            except Exception:
                pass
            try:
                page.get_by_text(pick, exact=False).first.click(timeout=2000)
                return True
            except Exception:
                pass
        try:
            target.select_option(label=val, timeout=3000)
            return True
        except Exception:
            pass
    except Exception:
        pass
    return False


def apply_field_value(
    page: Page,
    label: str,
    value: str,
    options: Optional[list[str]] = None,
) -> bool:
    """Write *value* into the field identified by *label* on *page*."""
    if not page or not label or not (value or "").strip():
        return False

    val = str(value).strip()
    clean = re.sub(r"\s*\*+\s*$", "", label.strip())

    if options:
        for loc in _locators_for_label(page, clean):
            if _try_select_option(page, loc, val, options):
                return True

    for loc in _locators_for_label(page, clean):
        try:
            if loc.count() == 0:
                continue
            first = loc.first
            if not first.is_visible(timeout=1500):
                continue
            role = ""
            try:
                role = (first.evaluate("el => (el.getAttribute('role') || el.tagName || '').toLowerCase()") or "")
            except Exception:
                pass
            is_select = False
            try:
                is_select = bool(
                    first.evaluate("el => (el.tagName || '').toUpperCase() === 'SELECT'")
                )
            except Exception:
                pass
            if "combobox" in role or "listbox" in role or is_select:
                if _try_select_option(page, first, val, options):
                    return True
            if humanized_fill(page, first, val):
                return True
        except Exception:
            continue

    # Radio yes/no: click option by accessible name
    low = val.lower()
    if low in ("yes", "no", "true", "false"):
        pick = "Yes" if low in ("yes", "true") else "No"
        try:
            grp = page.get_by_role("group", name=clean, exact=False).first
            if grp.count() > 0:
                grp.get_by_role("radio", name=pick, exact=False).first.click(timeout=3000)
                return True
        except Exception:
            pass

    return False


def diff_snapshots(
    before: dict[str, str],
    after: dict[str, str],
) -> list[tuple[str, str, str]]:
    """Return list of (label, old_value, new_value) for changed fields."""
    changes: list[tuple[str, str, str]] = []
    all_keys = set(before) | set(after)
    for key in all_keys:
        old = before.get(key, "")
        new = after.get(key, "")
        if old != new and new:
            changes.append((key, old, new))
    return changes


SubmissionChecker = Callable[[Page], tuple[bool, bool, dict[str, Any]]]
