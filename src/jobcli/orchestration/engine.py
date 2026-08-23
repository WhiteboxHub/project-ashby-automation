"""Core execution engine — unified agent loop with integrated human checkpoints.

Instead of a three-phase waterfall (LLM → Rules → Human), the engine runs a
single agent loop.  Human interaction is woven inline via ``AgentInterface``,
whose behaviour adapts to the configured ``InteractionMode`` (auto / supervised
/ manual) — integrating approval into its tool loop.
"""

import os
import random
import time
import re
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from jobcli.intelligence.memory import AgentMemory
    from jobcli.human.agent_interface import HandoffResult

from playwright.sync_api import Error, Page, sync_playwright

from jobcli.utils.browser_closed import BrowserClosed
from jobcli.utils.fill_guard import PLACEHOLDER_VALUES, is_meaningful_value
from jobcli.utils.logger import JobLogger, global_logger
from jobcli.profile.schemas import (
    ActionType,
    ApplicationState,
    ApplicationStatus,
    ATSType,
    BrowserAction,
    Config,
    ExecutionPhase,
    InteractionMode,
    Job,
    LLMActionResponse,
    ResumeData,
    CommonQuestions,
)
from jobcli.orchestration.tool_executor import ToolExecutor
from jobcli.human.agent_interface import AgentInterface
from jobcli.llm.client import LLMClient
from jobcli.llm.ax_tree_extractor import AccessibilityTreeExtractor
from jobcli.llm.empty_fields import build_empty_fields_payload
from jobcli.intelligence.memory_prefill import MemoryPrefiller
from jobcli.automation.anti_bot import AntiBotManager
from jobcli.ats.locators.apply_button import ApplyButtonLocator, adopt_application_page_after_action
from jobcli.ats.handlers.handler_factory import ATSHandlerFactory
from jobcli.ats.detector.ats_detector import ATSDetector
from jobcli.ats.locators.form_fields import FormFiller
from jobcli.storage.models import Database
from jobcli.storage.repositories import (
    ApplicationLogRepository,
    JobRepository,
    LearnedLocatorRepository,
    UserDataRepository,
)


def _strip_apply_clicks_when_filling_only(llm_response, task: str) -> None:
    """Avoid LLM repeatedly clicking Apply on the JD tab after we already adopted to ATS."""
    if task not in ("fill_form_fields_only", "fill_empty_fields_only"):
        return
    if not llm_response or not llm_response.actions:
        return

    pat = re.compile(r"(?i)(apply\s*now|submit\s*application|\bapply\b)")

    def looks_like_apply(a) -> bool:
        blob = " ".join(
            str(x)
            for x in (a.field_label, a.selector, a.value)
            if x
        )
        return bool(pat.search(blob))

    llm_response.actions = [
        a for a in llm_response.actions
        if not (a.action == ActionType.CLICK and looks_like_apply(a))
    ]


# Reject third-party / federated apply variants no matter what task the
# LLM was asked to do.  Belt-and-braces with the prompt rule + the
# rule-based locator's third-party filter.
#
# Two tiers:
#   1) UNAMBIGUOUS — these strings are never valid form answers, so we can
#      drop the click on sight:  "Easy Apply", "Apply with LinkedIn",
#      "Sign in with Google", etc.
#   2) BRAND-ONLY — a bare brand like "LinkedIn" is often a LEGITIMATE
#      dropdown option (e.g. "How did you hear about us?"), so we only
#      treat it as a third-party apply button when the surrounding
#      context also mentions apply/sign-in/continue.  Contexts that
#      strongly suggest it is an answer (e.g. "How did you hear",
#      "source", "referral") veto the drop entirely.
_THIRD_PARTY_APPLY_UNAMBIGUOUS_RE = re.compile(
    r"(?i)("
    r"easy\s*apply|"
    r"apply\s+(with|via|using|through|on)\s+|"
    r"sign\s*in\s+with\b|continue\s+with\b|\boauth\b|\bsso\b"
    r")"
)
_THIRD_PARTY_BRAND_RE = re.compile(
    r"(?i)\b(linkedin|indeed|glassdoor|ziprecruiter|monster|seek|naukri|xing|facebook)\b"
)
# Pattern that indicates the CLICK is answering a "how did you hear"-style
# question — in which case the brand name is a valid answer, not a third-
# party apply button.
_REFERRAL_SOURCE_CONTEXT_RE = re.compile(
    r"(?i)("
    r"how\s+did\s+you\s+hear|"
    r"where\s+did\s+you\s+hear|"
    r"hear\s+about|"
    r"referral|referred\s+by|"
    r"\bsource\b|"
    r"how\s+(do|did)\s+you\s+find|"
    r"found\s+us|"
    r"channel"
    r")"
)


def _looks_like_third_party_apply(blob: str) -> bool:
    """True if *blob* looks like a third-party apply / federated sign-in button."""
    if not blob:
        return False
    if _THIRD_PARTY_APPLY_UNAMBIGUOUS_RE.search(blob):
        return True
    # Brand name alone is ambiguous.  Only treat as third-party apply when
    # we see an apply/sign-in verb AND no "how did you hear" context.
    if _THIRD_PARTY_BRAND_RE.search(blob):
        if _REFERRAL_SOURCE_CONTEXT_RE.search(blob):
            return False
        if re.search(r"(?i)\b(apply|sign\s*in|log\s*in|continue)\b", blob):
            return True
    return False


def _safe_domain(url: str) -> str:
    """Extract the host (domain) from a URL, lowercased; safe on bad input."""
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        return host
    except Exception:
        return ""


# ──────────────────────────────────────────────────────────────────────
# Job-board (aggregator) detection.
# These are NOT applicant-tracking systems — they are listing / discovery
# sites that link out to the employer's real ATS.  The agent cannot
# submit an application from these domains, so we hand control to the
# human and ask them to click through to the real ATS tab before
# resuming.  This is deliberate, user-driven policy: do NOT try to
# auto-navigate these (they're gated behind login / bot-detection /
# Easy-Apply modals that we've explicitly committed not to automate).
# ──────────────────────────────────────────────────────────────────────
_JOB_BOARD_DOMAINS: tuple[str, ...] = (
    "linkedin.com", "www.linkedin.com",
    "indeed.com", "www.indeed.com", "in.indeed.com", "uk.indeed.com",
    "glassdoor.com", "www.glassdoor.com",
    "ziprecruiter.com", "www.ziprecruiter.com",
    "monster.com", "www.monster.com",
    "simplyhired.com", "www.simplyhired.com",
    "dice.com", "www.dice.com",
    "wellfound.com", "www.wellfound.com",
    "angel.co", "www.angel.co",
    "jobright.ai", "www.jobright.ai",
    "builtin.com", "www.builtin.com",
    "otta.com", "www.otta.com",
    "seek.com.au", "www.seek.com.au",
    "naukri.com", "www.naukri.com",
    "stepstone.com", "www.stepstone.com",
    "totaljobs.com", "www.totaljobs.com",
    "cv-library.co.uk", "www.cv-library.co.uk",
    "reed.co.uk", "www.reed.co.uk",
    "remote.co", "www.remote.co",
    "remoteok.com", "www.remoteok.com",
    "weworkremotely.com", "www.weworkremotely.com",
)


def _job_board_name(url: str) -> Optional[str]:
    """If *url* is a known job board / aggregator, return its pretty name.
    Otherwise return ``None``.  Used to decide whether to hand off to
    the human immediately instead of trying to drive the page."""
    host = _safe_domain(url)
    if not host:
        return None
    for board in _JOB_BOARD_DOMAINS:
        if host == board or host.endswith("." + board):
            # Return the shortest "pretty" form (strip www.)
            pretty = board.split(".")[0] if board.startswith("www.") else board
            return pretty.split(".")[0].title()  # "linkedin" → "Linkedin"
    return None


def _normalize_label(s: str) -> str:
    """Normalize a field label for comparison (strip *, whitespace, punctuation, case)."""
    if not s:
        return ""
    s = s.replace("*", " ").replace(":", " ").replace("?", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


from jobcli.utils.url_compare import urls_meaningfully_different as _urls_meaningfully_different


def _build_dropdown_label_set(ax_tree) -> dict[str, dict]:
    """Return ``{normalized_label: {options, type}}`` for every dropdown on the page.

    Generic across ALL ATS / sites — works off the accessibility tree:

    * Native ``<select>`` elements (``ax_tree.dropdown_fields[*].type == 'native_select'``).
    * Custom ARIA combobox / listbox / menu (any ``role`` of ``combobox``, ``listbox``).
    * Any form field whose role is ``combobox``, ``listbox``, or whose name matches a
      known dropdown in ``dropdown_fields`` (custom button-style dropdowns).

    The engine uses this to *coerce* LLM ``fill``/``type`` actions to ``select``
    when they target one of these labels (which is the #1 cause of "agent
    typing into a dropdown" failures across every ATS).
    """
    result: dict[str, dict] = {}

    for dp in (getattr(ax_tree, "dropdown_fields", None) or []):
        label = _normalize_label(dp.get("label", ""))
        if label:
            result[label] = {
                "options": dp.get("options", []) or [],
                "type": dp.get("type", "custom_dropdown"),
            }

    for f in (getattr(ax_tree, "form_fields", None) or []):
        role = (f.get("role") or "").lower()
        label = _normalize_label(f.get("name", "") or f.get("label", ""))
        if not label:
            continue
        if role in ("combobox", "listbox", "menu"):
            result.setdefault(label, {"options": [], "type": "aria_dropdown"})

    return result


def _coerce_dropdown_actions(llm_response, ax_tree, logger=None) -> int:
    """Coerce ``fill``/``type`` actions that target a dropdown label into ``select``.

    The LLM frequently emits ``action="fill"`` for fields that are actually
    dropdowns/comboboxes — typing into a closed dropdown does nothing on most
    sites and silently fails on Workday/Greenhouse/Ashby/etc.  This safety net
    rewrites the action *before* execution so the executor takes the proper
    open-dropdown-then-pick-option path.

    Generic implementation — works on every ATS that exposes dropdowns via
    ``<select>`` or ``role="combobox"``/``"listbox"``.

    Returns the number of actions coerced.
    """
    if not llm_response or not llm_response.actions:
        return 0

    dropdown_labels = _build_dropdown_label_set(ax_tree)
    if not dropdown_labels:
        return 0

    coerced = 0
    for a in llm_response.actions:
        if a.action not in (ActionType.FILL, ActionType.TYPE):
            continue
        candidates = [
            _normalize_label(a.field_label or ""),
            _normalize_label(a.selector or ""),
        ]
        match_label = None
        for cand in candidates:
            if not cand:
                continue
            if cand in dropdown_labels:
                match_label = cand
                break
            for dl in dropdown_labels:
                # Guard against empty / very-short dropdown labels that
                # would spuriously substring-match every candidate.
                # E.g. Lever embeds unlabeled <select> elements whose
                # normalized label is "" → `"" in "current location"` is
                # True in Python, coercing every FILL into SELECT.
                if not dl or len(dl) < 3:
                    continue
                if not cand or len(cand) < 3:
                    continue
                # Require a reasonable overlap ratio so a 2-char fragment
                # like "en" doesn't match "current location".
                shorter = min(len(cand), len(dl))
                longer = max(len(cand), len(dl))
                if shorter / longer < 0.35:
                    continue
                if cand in dl or dl in cand:
                    match_label = dl
                    break
            if match_label:
                break
        if not match_label:
            continue

        a.action = ActionType.SELECT
        coerced += 1
        if logger:
            logger.warning(
                f"Coerced FILL→SELECT for dropdown field '{a.field_label or a.selector}' "
                f"(value='{a.value}', options_known={len(dropdown_labels[match_label]['options'])})",
                phase=ExecutionPhase.LLM,
            )

    return coerced


def _empty_required_fields(ax_tree) -> list[str]:
    """Return labels of required fields that are still empty on the current page.

    A field is considered required if ``required=True`` OR its name contains
    an asterisk (``*``).  A field is considered empty if its value/checked
    state is empty/false.

    Generic across all ATS — uses ARIA properties and the asterisk convention.
    """
    if not ax_tree:
        return []
    empty: list[str] = []
    for f in (getattr(ax_tree, "form_fields", None) or []):
        label = (f.get("name") or f.get("label") or "").strip()
        if not label:
            continue
        is_required = bool(f.get("required")) or "*" in label
        if not is_required:
            continue

        role = (f.get("role") or "").lower()
        val = str(f.get("value", "") or "").strip()
        checked = f.get("checked")

        if role in ("checkbox", "radio", "switch"):
            if not (checked is True or (isinstance(checked, str) and checked.lower() in ("true", "on", "yes", "1"))):
                empty.append(label)
        else:
            if not val or val.lower() in ("select", "select...", "select an option", "choose", "please choose", "--"):
                empty.append(label)

    seen: set[str] = set()
    out: list[str] = []
    for lbl in empty:
        norm = _normalize_label(lbl)
        if norm in seen:
            continue
        seen.add(norm)
        out.append(lbl)
    return out


def _live_validation_errors(page) -> list[str]:
    """Scan the live DOM for visible validation-error messages.

    Complements ``_empty_required_fields`` which only sees what the
    accessibility-tree extractor produced: some ATSes (notably modern
    Greenhouse, Lever's React forms) render errors as plain ``<div>``
    nodes with classes like ``.error`` / ``.field-error`` and don't set
    ``aria-invalid`` on the input, so the AXTree reports the field as
    fine while the user sees a red "This field is required." message.

    Returns the *text* of each visible error so they can be surfaced
    in the handoff message.  Deduplicates and strips whitespace.
    """
    if page is None:
        return []
    # CSS classes commonly used for validation errors.  We intentionally
    # avoid very generic names ("red", "warning") that would false-match.
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
        # Run one ``evaluate_all`` so we do the collection+visibility probe
        # + text extraction in a single round-trip (far faster than
        # iterating with ``locator.all()``).
        js = r"""(sel) => {
            const out = [];
            const seen = new Set();
            document.querySelectorAll(sel).forEach((el) => {
                // Skip off-screen / display:none nodes — validation
                // errors for OTHER steps of a multi-step wizard may still
                // be in the DOM but not visible.
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return;
                const s = getComputedStyle(el);
                if (s.display === 'none' || s.visibility === 'hidden') return;
                const t = (el.innerText || el.textContent || '').trim();
                if (!t) return;
                // Reject very long text — probably a container, not the
                // error message itself.
                if t.length > 200) return;
                
                // Exclusion list: phrases that are NOT errors (success messages, 
                // help text, neutral status indicators).
                const lower = t.toLowerCase();
                if (lower.includes('success') || 
                    lower.includes('completed') || 
                    lower.includes('imported') || 
                    lower.includes('saved') ||
                    lower.includes('optional')) return;

                if (seen.has(t)) return;
                seen.add(t);
                out.push(t);
            });
            return out;
        }"""
        msgs = page.evaluate(js, selector) or []
    except Exception:
        return []
    return [str(m) for m in msgs if m]


_AUTH_URL_PATTERNS = (
    "/login", "/log-in", "/log_in", "/signin", "/sign-in", "/sign_in",
    "/signup", "/sign-up", "/sign_up", "/register", "/registration",
    "/authenticate", "/auth", "/oauth", "/sso",
    "/account/create", "/createaccount", "/create-account",
    "/myaccount", "/my-account",
    # Oracle HCM candidate experience — the "enter email" gate
    "/candidateexperience/",
    "/hcmui/candidateexperience",
    # Workday identity / sign-in
    "/wday/authgwy", "/wd5/",
)

# Phrases that, when they appear as a *field label* on the form being
# analysed, indicate an authentication / account-creation screen rather
# than a real job-application form.  Matched lowercased against the
# accessibility-tree label (no substring false-positive guard needed
# because these are all specific enough).
_AUTH_LABEL_KEYWORDS = (
    "password", "confirm password", "verify password",
    "re-enter password", "retype password", "create password",
    "new password", "old password", "current password",
    "verification code", "one-time code", "one time code",
    "otp ", "two-factor", "2fa",
)

# Phrases that, when present as a button label, are strong signals we
# are about to authenticate / create an account instead of apply.
_AUTH_ACTION_KEYWORDS = (
    "sign in", "log in", "log-in",
    "create account", "create an account", "register",
    "sign up",
)


def _is_auth_form(
    page, ax_tree
) -> tuple[bool, str]:
    """Detect whether the current page is an authentication / account-creation gate.

    Workday, Oracle HCM, iCIMS, and a handful of other ATSes drop the
    applicant on a "Create account or Sign in" wall *before* the real
    application form.  When that happens we must NOT let the LLM type
    anything: it will invent a password, submit real PII, and create a
    garbage account.  Instead, hand the browser to the human, let them
    log in with their own credentials, and resume once they are on the
    actual application page.

    Returns ``(is_auth, reason)``.  ``reason`` is a short human-readable
    description used in the handoff prompt.
    """
    # ── Signal 1: URL path — cheapest, highest precision ──
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    for pat in _AUTH_URL_PATTERNS:
        if pat in url:
            return True, f"URL path looks like an auth gate ({pat})."

    # ── Signal 2: any visible <input type=password> in the DOM ──
    # Strongest positive signal.  Workday's "Create account" screen has
    # ``<input type="password" data-automation-id="password">``.
    try:
        pw_count = page.evaluate(
            """() => {
                let n = 0;
                document.querySelectorAll("input[type='password']").forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) n++;
                });
                return n;
            }"""
        )
    except Exception:
        pw_count = 0
    if pw_count and pw_count > 0:
        return True, f"Found {pw_count} password field(s) on the page."

    # ── Signal 3: AX-tree form-field labels match auth vocabulary ──
    try:
        labels = [
            (f.get("label") or "").lower()
            for f in getattr(ax_tree, "form_fields", []) or []
        ]
    except Exception:
        labels = []
    auth_labels = [
        lbl for lbl in labels
        if any(kw in lbl for kw in _AUTH_LABEL_KEYWORDS)
    ]
    if auth_labels:
        return True, f"Form contains auth fields: {auth_labels[:3]}."

    # ── Signal 4: clickable buttons named "Sign in" / "Create account" ──
    # Only fires when there is ALSO no obvious application content: the
    # same page may show a "Sign in" link in a nav bar, which we don't
    # want to treat as a login wall.
    try:
        button_labels = [
            (el.get("label") or "").lower()
            for el in getattr(ax_tree, "clickable_elements", []) or []
        ]
    except Exception:
        button_labels = []
    auth_buttons = [
        b for b in button_labels
        if any(kw in b for kw in _AUTH_ACTION_KEYWORDS)
    ]
    # Only trip when auth buttons are present AND there are few real
    # form fields (less than 3 text/select inputs outside of email/password).
    non_auth_fields = [
        lbl for lbl in labels
        if lbl and not any(kw in lbl for kw in _AUTH_LABEL_KEYWORDS)
        and "email" not in lbl
    ]
    if auth_buttons and len(non_auth_fields) < 3:
        return True, (
            f"Page shows auth action buttons ({auth_buttons[:2]}) and "
            "no application form fields."
        )

    # ── Signal 5: Single-email-field gate ────────────────────────
    # Oracle HCM / some ATSes show a page with ONLY an email input
    # (+ maybe a checkbox) before the real application.  The email
    # is used to look up / create a candidate profile.  If the
    # human hasn't logged in yet, we should not auto-fill it.
    email_only_fields = [lbl for lbl in labels if "email" in lbl]
    non_email_fields = [
        lbl for lbl in labels
        if lbl and "email" not in lbl
        and not any(kw in lbl for kw in _AUTH_LABEL_KEYWORDS)
    ]
    if email_only_fields and len(non_email_fields) < 2:
        # Check page text for gate-like language
        try:
            page_text = (page.inner_text("body") or "")[:3000].lower()
        except Exception:
            page_text = ""
        gate_phrases = (
            "enter your email", "create a profile", "create an account",
            "sign in", "log in", "terms and conditions",
            "already have an account", "returning candidate",
            "create your profile",
        )
        if any(phrase in page_text for phrase in gate_phrases):
            return True, (
                "Single email field with gate language detected "
                "(likely a profile/login step)."
            )

    return False, ""


def _submit_button_visible(page) -> bool:
    """Return True iff a Submit/Apply-now button is currently on the page.

    Used post-submission to decide whether the form was accepted: if the
    button we just pressed is gone (and there are no validation errors),
    that's a very strong signal the ATS took the application even when
    it doesn't surface any "Thank you" text.  We check both typed
    ``<button type=submit>``/``<input type=submit>`` and role/label
    matches, because modern React-based ATSes often render submit as a
    ``<button>`` without a ``type``.
    """
    if page is None:
        return False
    js = r"""() => {
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
    try:
        return bool(page.evaluate(js))
    except Exception:
        # Playwright raised (page closing, navigation in flight, …).
        # Treat as "unknown / probably still there" so we don't falsely
        # claim success.
        return True


def _unselected_required_dropdowns(page) -> list[str]:
    """DOM-side scan for required dropdowns still showing a "Select…" placeholder.

    Greenhouse's new React application form renders every dropdown as a
    ``div[role='combobox']`` that displays ``"Select..."`` until the user
    picks an option.  These comboboxes expose neither ``required=true``
    nor a meaningful ``value`` on the accessibility tree, so the
    AXTree-based ``_empty_required_fields`` misses them entirely.

    The scan walks each visible *outer* combobox/listbox widget, ignores
    nested search-inputs (react-select keeps a hidden ``<input
    role='combobox'>`` that is always blank), checks for a chosen-value
    marker, and only reports labels with ``*`` / ``aria-required``.
    """
    if page is None:
        return []
    js = r"""() => {
        const out = [];
        const seen = new Set();
        // Placeholder text that indicates an unpicked dropdown.
        const PLACEHOLDERS = [
            'select...', 'select an option', 'choose...', 'please choose',
            'please select', 'choose one', '-- select --', '—', '--',
        ];
        // Class names react-select / chosen / ant-select use to store
        // the rendered "chosen option" text.  If any of these are
        // present and non-empty, the dropdown IS filled.
        const CHOSEN_VALUE_SEL = [
            '.select__single-value',
            '.select__multi-value__label',
            '.react-select__single-value',
            '.react-select__multi-value__label',
            '.chosen-single > span:not(.chosen-default)',
            '.ant-select-selection-item',
            '.Select-value-label',
            '[class*="singleValue" i]',
        ].join(',');
        // Class names used for the "placeholder" text.
        const PLACEHOLDER_SEL = [
            '.select__placeholder',
            '.react-select__placeholder',
            '.chosen-default',
            '.ant-select-selection-placeholder',
            '.Select-placeholder',
            '[class*="placeholder" i]',
        ].join(',');

        const isVisible = (el) => {
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) return false;
            const s = getComputedStyle(el);
            return s.display !== 'none' && s.visibility !== 'hidden';
        };
        const findLabel = (el) => {
            const labelledby = el.getAttribute('aria-labelledby');
            if (labelledby) {
                const parts = labelledby.split(/\s+/)
                    .map(id => document.getElementById(id))
                    .filter(Boolean)
                    .map(n => (n.innerText || n.textContent || '').trim())
                    .filter(Boolean);
                if (parts.length) return parts.join(' ');
            }
            const al = (el.getAttribute('aria-label') || '').trim();
            if (al) return al;
            if (el.id) {
                const lbl = document.querySelector(`label[for="${el.id}"]`);
                if (lbl) return (lbl.innerText || lbl.textContent || '').trim();
            }
            let parent = el.parentElement;
            for (let i = 0; i < 6 && parent; i++) {
                const lbl = parent.querySelector(':scope > label, :scope > legend, :scope > .label, :scope > [class*="label" i]');
                if (lbl) {
                    const t = (lbl.innerText || lbl.textContent || '').trim();
                    if (t) return t;
                }
                parent = parent.parentElement;
            }
            return '';
        };
        const hasChosenValue = (el) => {
            // If a "single-value"/multi-value label node exists and
            // contains text, the dropdown IS filled.  This is the
            // most reliable signal on react-select / ant-select.
            const chosen = el.querySelector(CHOSEN_VALUE_SEL);
            if (chosen) {
                const t = (chosen.innerText || chosen.textContent || '').trim();
                if (t) return true;
            }
            return false;
        };
        const looksPlaceholder = (text) => {
            if (!text) return true;
            const t = text.trim().toLowerCase();
            if (!t) return true;
            return PLACEHOLDERS.some(p => t === p || t.startsWith(p));
        };

        const combos = document.querySelectorAll(
            "[role='combobox'], [role='listbox'], .select__control, " +
            ".react-select__control, .chosen-single, .ant-select-selector, " +
            ".Select-control"
        );
        combos.forEach((el) => {
            // Skip hidden <input role='combobox'> — react-select and
            // many other libs keep a permanently empty search input
            // inside the actual dropdown container.  We only want the
            // outermost, visible widget.
            if (el.tagName === 'INPUT') return;
            if (!isVisible(el)) return;
            // Skip inner nested combobox/listbox nodes: if an ancestor
            // we've already visited was a combobox, this is a duplicate.
            const ancestorCombo = el.parentElement &&
                el.parentElement.closest(
                    "[role='combobox'], [role='listbox'], .select__control, " +
                    ".react-select__control, .chosen-single, .ant-select-selector, " +
                    ".Select-control"
                );
            if (ancestorCombo && ancestorCombo !== el) return;

            // If it has a chosen-value node with text, it's definitely
            // filled — skip regardless of what the outer innerText looks
            // like (which can include "× ▼" decorations on some libs).
            if (hasChosenValue(el)) return;

            // Otherwise, compute a cleaned text that ignores placeholder
            // nodes and the X/caret buttons.
            let text = '';
            try {
                const clone = el.cloneNode(true);
                clone.querySelectorAll(PLACEHOLDER_SEL).forEach(
                    p => p.remove()
                );
                clone.querySelectorAll(
                    "[aria-hidden='true'], button, svg, " +
                    ".select__indicators, .select__indicator, " +
                    ".react-select__indicators, .react-select__indicator, " +
                    ".chosen-search, .chosen-search-input, " +
                    ".ant-select-arrow, .ant-select-clear, " +
                    ".Select-arrow-zone, .Select-clear-zone"
                ).forEach(n => n.remove());
                text = (clone.innerText || clone.textContent || '').trim();
            } catch (_) {
                text = (el.innerText || el.textContent || '').trim();
            }

            if (!looksPlaceholder(text)) return;

            const label = findLabel(el);
            if (!label) return;
            const required = label.includes('*') ||
                el.getAttribute('aria-required') === 'true' ||
                (el.closest("[aria-required='true']") !== null);
            if (!required) return;
            const clean = label.replace(/\s+/g, ' ').trim();
            if (seen.has(clean)) return;
            seen.add(clean);
            out.push(clean);
        });
        return out;
    }"""
    try:
        labels = page.evaluate(js) or []
    except Exception:
        return []
    return [str(l) for l in labels if l]


_NEXT_BUTTON_RE = re.compile(
    r"(?i)\b(next|continue|proceed|save\s*&?\s*continue|review|submit|submit\s+application|apply|finish|complete)\b"
)


def _split_off_advance_clicks(llm_response) -> tuple[list, list]:
    """Split LLM actions into ``(non_advance, advance_clicks)``.

    "Advance" clicks = Next / Continue / Submit / Apply / Review / Finish.
    These are the buttons that move the user to the *next* page or submit
    the application.  The engine holds them back when required fields are
    still empty so the human can fill them first.
    """
    if not llm_response or not llm_response.actions:
        return [], []

    advance, rest = [], []
    for a in llm_response.actions:
        if a.action != ActionType.CLICK:
            rest.append(a)
            continue
        blob = " ".join(str(x) for x in (a.field_label, a.selector, a.value) if x)
        if _NEXT_BUTTON_RE.search(blob):
            advance.append(a)
        else:
            rest.append(a)
    return rest, advance


def _strip_third_party_apply_clicks(llm_response, logger=None) -> None:
    """Drop any CLICK action that targets a third-party apply / sign-in button.

    The LLM is instructed not to emit these (see ``_build_axtree_prompt``)
    but we filter again as a safety net.
    """
    if not llm_response or not llm_response.actions:
        return
    kept = []
    for a in llm_response.actions:
        if a.action == ActionType.CLICK:
            blob = " ".join(str(x) for x in (a.field_label, a.selector, a.value) if x)
            if _looks_like_third_party_apply(blob):
                if logger:
                    logger.warning(
                        f"Dropping third-party apply click proposed by LLM: '{blob[:100]}'",
                        phase=ExecutionPhase.LLM,
                    )
                continue
        kept.append(a)
    llm_response.actions = kept


class ApplicationEngine:
    """Core engine for job application automation."""

    def __init__(
        self,
        config: Config,
        resume: ResumeData,
        database: Database,
        on_event: Optional[Any] = None,
    ) -> None:
        """Initialize engine."""
        self.config = config
        self.resume = resume
        self.database = database
        self.on_event = on_event
        self.active_agent: Optional[AgentInterface] = None
        self.session = database.get_session()

        # Initialize repositories
        self.job_repo = JobRepository(self.session)
        self.log_repo = ApplicationLogRepository(self.session)
        self.locator_repo = LearnedLocatorRepository(self.session)
        self.user_data_repo = UserDataRepository(self.session)

        # Cached extension payloads (JSON Resume + PDF blob); loaded per apply run.
        self._extension_resume_data: Optional[dict] = None
        self._extension_resume_file: Optional[dict] = None
        self._extension_normalized_data: Optional[dict] = None
        self.common_questions: Optional[CommonQuestions] = None

        self.anti_bot = AntiBotManager()
        self.stop_requested = False
        
        # Browser session state
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.active_page: Any = None
        self.user_data_dir: Any = None

    def _resolve_extension_dir(self) -> Optional[str]:
        """Pick a valid TalentScreen extension directory or return ``None``.

        Delegates to :func:`jobcli.utils.extension_helpers.resolve_extension_dir`
        which checks:
        1. ``config.extension_path``
        2. ``~/.jobcli/extension_unpacked``
        3. Newest ``extension/*.zip`` (unpack to ``~/.jobcli/``)
        4. ``<project-root>/bin/project-talentscreen-autofill-extension``

        The resolved path is persisted back so subsequent runs skip the search.
        """
        from jobcli.utils.extension_helpers import (
            get_local_extension_zip,
            maybe_install_local_extension_zip,
            resolve_extension_dir,
        )

        if get_local_extension_zip():
            maybe_install_local_extension_zip()

        configured = (self.config.extension_path or "").strip()
        resolved = resolve_extension_dir(configured or None)

        if resolved and resolved != configured:
            global_logger.info(
                f"Extension resolved: {resolved} (was {configured!r}). Updating saved config."
            )
            self._persist_extension_path(resolved)
        elif resolved:
            global_logger.info(f"Extension found at {resolved}")
        else:
            global_logger.warning("No valid extension found. Form autofill will rely on Python rules + LLM only.")

        return resolved

    def _persist_extension_path(self, resolved: str) -> None:
        """Update ``self.config.extension_path`` in memory and on disk.

        Done via ``jobcli.cli.main.save_config`` so the write goes through
        the same code path as the interactive commands (``login``,
        ``setup``). Failures are non-fatal — the in-memory update is
        always applied so the current session still uses the resolved
        path even if the DB write fails (e.g. SQLite is locked).
        """
        self.config.extension_path = resolved
        try:
            from jobcli.cli.main import save_config
            save_config(self.config)
        except Exception as e:
            global_logger.warning(
                f"Could not persist resolved extension_path to DB: {e}. "
                "It will still be used for the current session."
            )

    def start_session(self) -> None:
        """Start a single browser session for the duration of the batch."""
        if self.context:
            return
            
        from playwright.sync_api import sync_playwright
        from jobcli.automation.stealth import (
            LAUNCH_ARGS,
            IGNORE_DEFAULT_ARGS,
            CONTEXT_OPTIONS,
        )
        
        self.playwright = sync_playwright().start()

        # Resolve the TalentScreen extension directory with three fallback
        # tiers, so the engine remains usable even when ``config.extension_path``
        # is stale (e.g. the DB was copied from another machine where the
        # path was ``C:\Users\OTHER\.jobcli\extension_unpacked\`` and that
        # path does not exist locally). Without this auto-recovery the
        # browser silently launched without the extension and form
        # autofill was 100% dependent on the LLM — which was the failure
        # the user reported.
        #
        # Order (each tier validated by ``manifest.json`` presence, since
        # an empty / partial directory is worse than no extension at all):
        #   1. ``config.extension_path`` (what ``jobcli setup`` wrote)
        #   2. ``~/.jobcli/extension_unpacked`` — the canonical default
        #   3. Auto-download via :class:`ExtensionManager` on the fly
        extension_dir = self._resolve_extension_dir()
        self.extension_dir = extension_dir
        launch_args = list(LAUNCH_ARGS)
        
        if self.extension_dir and os.path.exists(self.extension_dir):
            import tempfile
            self.user_data_dir = tempfile.mkdtemp(prefix="jobcli_ext_profile_")
            launch_args.extend([
                f"--disable-extensions-except={self.extension_dir}",
                f"--load-extension={self.extension_dir}"
            ])
            global_logger.info(f"Launching persistent browser context with extension from: {self.extension_dir}")
            self.context = self.playwright.chromium.launch_persistent_context(
                self.user_data_dir,
                headless=False,
                args=launch_args,
                ignore_default_args=IGNORE_DEFAULT_ARGS,
                **CONTEXT_OPTIONS,
            )
        else:
            global_logger.info("Launching standard browser (no extension path provided or not found).")
            # Browser is always visible while applying — required for human-in-the-loop checkpoints
            # and to let the user watch automation in real time.
            self.browser = self.playwright.chromium.launch(
                headless=False,
                args=launch_args,
                ignore_default_args=IGNORE_DEFAULT_ARGS,
            )
            self.context = self.browser.new_context(
                **CONTEXT_OPTIONS,
            )

    def stop_session(self) -> None:
        """Stop the browser session."""
        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self.playwright:
            try:
                self.playwright.stop()
            except Exception:
                pass
            self.playwright = None

    def request_stop(self) -> None:
        """Signal the engine to stop as soon as possible."""
        self.stop_requested = True
        if self.active_agent:
            self.active_agent.remote_resume("cancel")

    def _check_stop(self) -> None:
        """Raise if a stop was requested via the dashboard or by user exit.

        Two stop sources:
          * ``self.stop_requested`` — set by the server-side ``request_stop``
            (Stop button in the dashboard UI). Raises ``InterruptedError``
            so the existing engine try/except path captures it.
          * Global ``is_exit_requested()`` — set by the SIGINT handler in
            :mod:`jobcli.utils.exit_signal` when the user pressed Ctrl+C.
            Raises ``ExitRequested`` which bypasses the engine's
            ``except Exception`` catch-alls (BaseException subclass) and
            propagates to the CLI's apply loop for graceful shutdown.
        """
        if self.stop_requested:
            raise InterruptedError("Process stopped by user")
        from jobcli.utils.exit_signal import ExitRequested, is_exit_requested
        if is_exit_requested():
            raise ExitRequested("Ctrl+C detected at engine checkpoint")

    def _emit_event(self, event_type: str, data: Any) -> None:
        """Emit an event to the callback if configured."""
        if self.on_event:
            try:
                self.on_event({"type": event_type, "data": data})
            except Exception:
                pass

    def _set_workday_modal_resume_path(self, handler: Any) -> None:
        """Attach the resume PDF path so :class:`WorkdayHandler` can pick *Autofill* first."""
        p = getattr(self.config, "resume_pdf_path", None)
        if not p or not str(p).strip():
            return
        if handler is not None and handler.__class__.__name__ == "WorkdayHandler":
            setattr(handler, "resume_path_for_workday_modal", str(p))

    def _get_llm_client(self, logger: Optional[JobLogger] = None) -> Optional[LLMClient]:
        """Initialize LLMClient based on current config."""
        provider = self.config.default_llm_provider
        api_key = None
        if provider == "openai":
            api_key = self.config.openai_api_key
        elif provider == "anthropic":
            api_key = self.config.anthropic_api_key
        elif provider == "gemini":
            api_key = self.config.gemini_api_key
        
        if not api_key:
            return None
            
        return LLMClient(provider, api_key, logger)

    # ------------------------------------------------------------------
    # CAPTCHA / bot-challenge freeze gate
    # ------------------------------------------------------------------
    def _freeze_if_verification(
        self,
        page,
        agent: AgentInterface,
        logger: JobLogger,
        *,
        context_label: str,
    ) -> bool:
        """If the current page is showing a CAPTCHA / "verify you are human"
        challenge, freeze all automation and hand control to the human.

        Any programmatic scrolling / clicking / AX-tree extraction while a
        challenge is running tends to re-trigger the bot signal and break
        verification (Cloudflare Turnstile, hCaptcha "invisible", etc.).  So
        on detection we:

          1. Stop issuing any Playwright commands besides URL / title reads.
          2. Show a prominent browser overlay + terminal modal + bell.
          3. Wait for the human to press ENTER.
          4. Poll ``wait_until_cleared`` until the challenge actually
             disappears (some challenges take a moment to finalise cookies
             after the user interacts).
          5. Return True if the page is now clear, False if the user
             cancelled or the challenge never cleared.

        Returns True if automation may continue; False if the caller should
        abort this job.
        """
        try:
            if not self.anti_bot.detect_captcha(page):
                return True
        except Exception:
            return True

        logger.warning(
            f"Bot challenge / CAPTCHA detected ({context_label}) — freezing automation.",
            phase=ExecutionPhase.RULES,
        )
        try:
            logger.capture_screenshot(page, f"captcha_{context_label}", ExecutionPhase.RULES)
        except Exception:
            pass

        # Block here — handle_captcha shows the browser overlay + terminal
        # modal + bell and waits for ENTER.
        solved = agent.handle_captcha()
        if not solved:
            return False

        # The human pressed ENTER.  Poll to make sure the challenge actually
        # cleared; Cloudflare sometimes flips the cookie a second or two
        # later after the user has already solved the puzzle.
        cleared = self.anti_bot.wait_until_cleared(page, max_wait_seconds=30)
        if not cleared:
            agent.show_warning(
                "Verification challenge is still visible. Finish it in the browser, "
                "then press ENTER again."
            )
            # Second chance — some challenges chain two steps.
            solved = agent.handle_captcha()
            if not solved:
                return False
            cleared = self.anti_bot.wait_until_cleared(page, max_wait_seconds=45)

        if cleared:
            agent.show_success("Verification cleared — resuming automation.")
        return cleared

    def _handoff_for_job_board(
        self,
        page,
        agent: AgentInterface,
        logger: JobLogger,
        *,
        board: str,
    ) -> Optional[Page]:
        """Hand off when the landing page is a job board (LinkedIn, JobRight, …).

        Returns the ``Page`` the human ended up on (could be a new tab), or
        ``None`` if the human cancelled.  The caller should treat ``None``
        as a terminal failure for this job.
        """
        page_ids_before = {id(p) for p in page.context.pages}
        page_count_before = len(page.context.pages)
        url_before = ""
        try:
            url_before = page.url or ""
        except Exception:
            pass

        logger.info(
            f"Job-board URL detected ({board}) — handing off to human.",
            phase=ExecutionPhase.HUMAN,
            board=board,
            url=url_before,
        )
        # If in AUTO mode, let the LLM try to navigate the board before handing off
        if agent.mode == InteractionMode.AUTO:
            agent.show_status(f"Attempting autonomous navigation on {board}...", phase=ExecutionPhase.LLM)
            navigated_page = self._navigate_job_board_via_llm(page, agent, logger, board)
            if navigated_page:
                # Successfully landed on a new domain!
                return navigated_page

        agent.show_warning(
            f"{board} is a job board, not an ATS. The agent typically can't submit "
            "applications from here without reaching the company site."
        )

        hint = (
            f"On {board}, find and click the employer's real Apply button "
            "(often labelled 'Apply on company site' or similar) — NOT "
            "'Easy Apply'. It will usually open a new tab on the company's "
            "ATS (Greenhouse / Ashby / Workday / …). Once you're on the "
            "ATS application page, press ENTER here."
        )
        result = agent.handoff_to_human(
            reason=f"URL is on {board} (a job board). "
                   "Please click through to the company's real application page.",
            hint=hint,
            wait_for_navigation_seconds=10,
        )
        if self._handoff_skipped_job(result, agent, logger, ExecutionPhase.HUMAN):
            return None
        if result.cancelled:
            agent.show_error("Cancelled by user at job-board handoff.")
            return None

        # The human may have opened a new tab (most common on LinkedIn /
        # Indeed: "Apply on company site" opens a popup).  Use the existing
        # tab-adoption helper to pick the best page to automate from here.
        adopted = adopt_application_page_after_action(
            page,
            page_count_before=page_count_before,
            url_before=url_before,
            page_ids_before=page_ids_before,
            logger=logger,
            poll_seconds=6.0,
        )
        new_page = adopted if adopted is not None else page
        try:
            new_page.bring_to_front()
        except Exception:
            pass
        try:
            new_url = new_page.url or ""
        except Exception:
            new_url = ""

        # Still on a job board?  That means the human either didn't move
        # off or ended up on a different aggregator (e.g. LinkedIn →
        # Indeed redirect).  Keep prompting until they get to a real ATS
        # page or cancel.  Limit to 3 tries to avoid infinite loops.
        attempts = 0
        while _job_board_name(new_url) and attempts < 2:
            attempts += 1
            agent.show_warning(
                f"Still on a job board ({_job_board_name(new_url)}). "
                "Please click through to the company's real ATS page."
            )
            page_ids_before = {id(p) for p in new_page.context.pages}
            page_count_before = len(new_page.context.pages)
            url_before = new_url
            result = agent.handoff_to_human(
                reason="Still on a job board — need to reach the company's ATS.",
                hint=hint,
                wait_for_navigation_seconds=10,
            )
            if self._handoff_skipped_job(result, agent, logger, ExecutionPhase.HUMAN):
                return None
            if result.cancelled:
                return None
            adopted = adopt_application_page_after_action(
                new_page,
                page_count_before=page_count_before,
                url_before=url_before,
                page_ids_before=page_ids_before,
                logger=logger,
                poll_seconds=6.0,
            )
            new_page = adopted if adopted is not None else new_page
            try:
                new_url = new_page.url or ""
            except Exception:
                new_url = ""

        if _job_board_name(new_url):
            agent.show_error(
                f"Still on a job board after {attempts + 1} attempts. "
                "Aborting — please re-run with the ATS URL directly."
            )
            return None

        logger.info(
            "Resuming on ATS page handed to us by the human.",
            phase=ExecutionPhase.HUMAN,
            url=new_url[:200],
        )
        agent.show_success(f"Resuming on ATS page: {new_url[:120]}")
        return new_page

    def apply_to_job(self, job: Job) -> ApplicationStatus:
        """Apply to a single job using a unified agent loop with inline human checkpoints."""
        global_logger.info(f"Starting application for job {job.id}", job_url=job.url)

        logger = JobLogger(
            job_id=job.id or 0,
            log_directory=self.config.log_directory,
            enable_screenshots=self.config.screenshot_on_error,
            on_event=self.on_event,
        )

        state = ApplicationState(
            job_id=job.id or 0,
            current_url=job.url,
        )

        self._submit_declined_by_user = False
        self._job_skipped_by_user = False
        self._form_ready_for_submit = False

        mode = self.config.interaction_mode
        self.common_questions = self.user_data_repo.get_questions()
        
        # ── Resume Path Validation ──────────────────────────────────────
        # Ensure we only pass a path that actually exists on this system.
        resume_pdf_path = self.config.resume_pdf_path
        if resume_pdf_path and not os.path.exists(resume_pdf_path):
            global_logger.warning(
                f"Resume PDF not found at '{resume_pdf_path}'. "
                "The agent will proceed without a file attachment."
            )
            resume_pdf_path = None

        # Ensure session is started
        if not self.context:
            self.start_session()
            
        # Reuse existing page if available, otherwise create new one
        if not hasattr(self, 'active_page') or self.active_page is None or self.active_page.is_closed():
            old_pages = [p for p in self.context.pages if p.url in ("about:blank", "chrome://newtab/")]
            self.active_page = self.context.new_page()
            for p in old_pages:
                try:
                    p.close()
                except Exception:
                    pass
            
        page = self.active_page
        status = ApplicationStatus.IN_PROGRESS
        state.status = ApplicationStatus.IN_PROGRESS
        try:
            if self.config.headless:
                from jobcli.automation.stealth import apply_stealth
                apply_stealth(self.context, logger=logger)
            self.anti_bot.logger = logger

            # Pre-load resume into extension storage (fill is triggered on the
            # application form in _run_extension_autofill_phase, before the LLM).
            self._inject_resume_into_extension(page, logger, trigger_fill=False)

            # AgentInterface is created once and used throughout the loop.
            # Memory + ATS type are populated as soon as we know them so every
            # human prompt can check the DB first.
            agent = AgentInterface(
                page,
                self.locator_repo,
                mode=mode,
                logger=logger,
                resume=self.resume,
                is_server=self.on_event is not None,
            )
            agent.common_questions = self.common_questions
            self.active_agent = agent
            self.stop_requested = False # Reset for new job

            self._check_stop()
            # ── 1. Navigate ─────────────────────────────────────────
            agent.show_phase_banner("Navigating to job page")
            import playwright.sync_api
            try:
                page.goto(job.url, timeout=45000, wait_until="domcontentloaded")
            except playwright.sync_api.TimeoutError:
                logger.warning("Page load timed out after 45s. Continuing anyway.", phase=ExecutionPhase.RULES)
            self._random_delay()

            # --- SKIP LOGIC: LinkedIn and Workday via URL ---
            current_url = page.url.lower()
            orig_url = job.url.lower()
            if "linkedin.com" in current_url or "linkedin.com" in orig_url:
                agent.show_warning("LinkedIn job detected.")
                should_skip = agent.ask_yes_no("  This is a LinkedIn job. Should I skip it?", default=True)
                
                if should_skip:
                    logger.info("Skipping LinkedIn job as confirmed by user", phase=ExecutionPhase.RULES)
                    logger.log_phase_end(ExecutionPhase.RULES, True)
                    return ApplicationStatus.SKIPPED
                else:
                    agent.show_status("Handing off to you for manual application...", phase=ExecutionPhase.HUMAN)
                    handoff = agent.handoff_to_human(
                        reason="LinkedIn job detected. Please apply manually in the browser.",
                        hint="When you're finished with the application, press ENTER to move to the next job."
                    )
                    if self._handoff_skipped_job(
                        handoff, agent, logger, ExecutionPhase.HUMAN
                    ):
                        self.job_repo.update_status(
                            job.id or 0, ApplicationStatus.SKIPPED
                        )
                        return ApplicationStatus.SKIPPED
                    if handoff.cancelled:
                        return ApplicationStatus.FAILED
                    return ApplicationStatus.SUBMITTED
                
            if "myworkdayjobs.com" in current_url or "myworkdayjobs.com" in orig_url:
                agent.show_warning("Workday job detected via URL - skipping as requested.")
                logger.info("Skipping Workday job (URL check)", phase=ExecutionPhase.RULES)
                logger.log_phase_end(ExecutionPhase.RULES, True)
                return ApplicationStatus.SKIPPED

            # ── 1b. Freeze immediately if we landed on a CAPTCHA ────
            # This MUST run before cookie-consent dismissal and any
            # other DOM manipulation.  Clicking cookie buttons, probing
            # elements, or injecting scripts while a bot challenge is
            # active tends to flip the challenge into a permanent
            # "access denied" state.
            if not self._freeze_if_verification(
                page, agent, logger, context_label="post_navigate"
            ):
                self.job_repo.update_status(job.id or 0, ApplicationStatus.FAILED)
                return ApplicationStatus.FAILED
            self._dismiss_cookie_consent(page, logger)
            logger.capture_screenshot(page, "initial", ExecutionPhase.RULES)
            # ── 1c. Job-board (LinkedIn / JobRight / Indeed / …) handoff ──
            # These sites are job-aggregators, NOT ATSes.  The agent can't
            # submit an application from them — the real application lives
            # on the employer's ATS, reached either by clicking "Apply on
            # company site" (opens a new tab) or by following a redirect
            # from "Easy Apply" (which we explicitly forbid anyway).
            # Hand control to the human, let them click through, and then
            # adopt whatever tab/page they land on as the new target.
            board = _job_board_name(page.url or job.url)
            if board:
                page = self._handoff_for_job_board(
                    page, agent, logger, board=board
                )
                if page is None:
                    if getattr(self, "_job_skipped_by_user", False):
                        self.job_repo.update_status(
                            job.id or 0, ApplicationStatus.SKIPPED
                        )
                        return ApplicationStatus.SKIPPED
                    self.job_repo.update_status(
                        job.id or 0, ApplicationStatus.FAILED
                    )
                    return ApplicationStatus.FAILED
                # Keep AgentInterface / state pointing at the page the
                # human handed us.
                agent.page = page
                # Also re-check for a verification challenge on the new
                # page — the employer's ATS may itself be gated.
                if not self._freeze_if_verification(
                    page, agent, logger, context_label="post_jobboard_handoff"
                ):
                    self.job_repo.update_status(
                        job.id or 0, ApplicationStatus.FAILED
                    )
                    return ApplicationStatus.FAILED
                self._dismiss_cookie_consent(page, logger)
                logger.capture_screenshot(
                    page, "after_jobboard_handoff", ExecutionPhase.HUMAN
                )
            # ── 2. Detect ATS ───────────────────────────────────────
            detector = ATSDetector(page, logger)
            # Always detect from the CURRENT page URL (may differ from the
            # original job.url after a job-board handoff).
            current_url = page.url or job.url
            ats_type = detector.detect(current_url)
            state.detected_ats = ats_type
            self.job_repo.update_ats_type(job.id or 0, ats_type)
            agent.set_context(ats_type=ats_type)
            agent.show_status(f"Detected ATS: {ats_type.value}", phase=ExecutionPhase.RULES)

            if ats_type == ATSType.WORKDAY:
                agent.show_warning("Workday ATS detected - skipping as requested.")
                logger.info("Skipping Workday job (ATS match)", phase=ExecutionPhase.RULES)
                logger.log_phase_end(ExecutionPhase.RULES, True)
                return ApplicationStatus.SKIPPED

            # ── 2b. Check for Expired Job ───────────────────────────
            handler = ATSHandlerFactory.create_handler(ats_type, page, self.resume, logger)
            if handler.is_expired():
                agent.show_warning("This job posting has expired — skipping.")
                logger.info("Job expired, skipping.", phase=ExecutionPhase.RULES)
                self.job_repo.update_status(job.id or 0, ApplicationStatus.SKIPPED)
                return ApplicationStatus.SKIPPED

            # ── 3. Click Apply (auto, with inline human fallback) ───
            # First, check if the page already IS the application form
            # (e.g. file:// test pages, direct application links, or
            # sites that land directly on the form without an Apply button).
            page_already_has_form = False
            try:
                current_url = (page.url or "").lower()
                if "/application" in current_url or "/apply" in current_url or "ashbyhq.com" in current_url:
                    page_already_has_form = True
                    logger.info(
                        f"Direct application URL detected ({current_url[:60]}) — proceeding directly to form fill.",
                        phase=ExecutionPhase.RULES,
                    )
                else:
                    visible_inputs = page.locator(
                        "input:not([type='hidden']):not([type='submit']), select, textarea, [class*='application-form']"
                    ).count()
                    if visible_inputs >= 1:
                        page_already_has_form = True
                        logger.info(
                            f"Detected {visible_inputs} form fields — "
                            f"skipping Apply button search, page is already a form.",
                            phase=ExecutionPhase.RULES,
                        )
            except Exception:
                pass
            if page_already_has_form:
                apply_clicked = True
            else:
                agent.show_phase_banner("Finding and clicking Apply button")
                # A verification challenge may appear *between* pages (job
                # board → ATS redirect) — check again right before we touch
                # the DOM looking for an Apply button.
                if not self._freeze_if_verification(
                    page, agent, logger, context_label="pre_apply_click"
                ):
                    self.job_repo.update_status(job.id or 0, ApplicationStatus.FAILED)
                    return ApplicationStatus.FAILED
                apply_clicked, page = self._click_apply_button(page, state, logger, ats_type)
                # Page may have changed (new tab) — update agent's reference
                agent.page = page
                # Clicking Apply often takes us to a gated ATS page that
                # itself shows a CAPTCHA. Freeze before moving on.
                if apply_clicked:
                    if not self._freeze_if_verification(
                        page, agent, logger, context_label="post_apply_click"
                    ):
                        self.job_repo.update_status(job.id or 0, ApplicationStatus.FAILED)
                        return ApplicationStatus.FAILED
            if not apply_clicked:
                agent.show_warning("Could not find Apply button automatically.")
                # Hand the browser to the human directly.  We used to
                # show a terminal-only selector picker here, but it
                # was confusing and rarely useful — the native
                # browser handoff is always the better UX.
                handoff = agent.handoff_to_human(
                    reason="Could not find a native Apply button on this page.",
                    hint="Click the correct Apply button yourself (avoid 'Apply with LinkedIn/Indeed'), "
                         "or navigate to the application form. Then press ENTER to hand control back.",
                )
                if self._handoff_skipped_job(
                    handoff, agent, logger, ExecutionPhase.HUMAN
                ):
                    self.job_repo.update_status(job.id or 0, ApplicationStatus.SKIPPED)
                    return ApplicationStatus.SKIPPED
                if handoff.cancelled:
                    self.job_repo.update_status(job.id or 0, ApplicationStatus.FAILED)
                    return ApplicationStatus.FAILED
                page = handoff.page
                agent.page = page
                # If the human navigated forward, treat the apply step
                # as already completed — do NOT go back. Re-detect ATS
                # in case they landed on an entirely different host
                # (e.g. company site -> Workday/Greenhouse).
                if handoff.advanced:
                    apply_clicked = True
                    try:
                        new_ats = ATSDetector(page, logger).detect(page.url)
                        if new_ats != ats_type:
                            ats_type = new_ats
                            state.detected_ats = ats_type
                            self.job_repo.update_ats_type(job.id or 0, ats_type)
                            agent.set_context(ats_type=ats_type)
                            agent.show_status(
                                f"Re-detected ATS after handoff: {ats_type.value}",
                                phase=ExecutionPhase.RULES,
                            )
                    except Exception:
                        pass
                    logger.capture_screenshot(page, "human_apply_resume", ExecutionPhase.HUMAN)
            else:
                agent.show_success("Apply button clicked.")
            if apply_clicked:
                page.wait_for_timeout(2000)
            # ── 4. Fill form — unified agent loop ───────────────────
            self._check_stop()
            agent.show_phase_banner("Filling application form")
            success = self._agent_fill_loop(page, state, logger, agent, apply_was_clicked=apply_clicked, resume_pdf_path=resume_pdf_path)
            if getattr(self, "_job_skipped_by_user", False):
                self.job_repo.update_status(job.id or 0, ApplicationStatus.SKIPPED)
                agent.show_warning("Skipped — opening next job.")
                return ApplicationStatus.SKIPPED
            if not success and state.step_count == 0:
                self._check_stop()
                # Rules-based fallback only if AI made zero progress
                agent.show_status("AI made no progress — trying rule-based fill...", phase=ExecutionPhase.RULES)
                success = self._fill_form_rules(page, state, logger, ats_type)
            if not success:
                self._check_stop()
                if getattr(self, "_job_skipped_by_user", False):
                    self.job_repo.update_status(job.id or 0, ApplicationStatus.SKIPPED)
                    return ApplicationStatus.SKIPPED
                if getattr(self, "_submit_declined_by_user", False):
                    agent.show_status(
                        "Submission skipped — moving to the next job.",
                        phase=ExecutionPhase.HUMAN,
                    )
                    self.job_repo.update_status(job.id or 0, ApplicationStatus.SKIPPED)
                    return ApplicationStatus.SKIPPED
                agent.show_warning("Automation could not complete the form.")
                handoff = self._handoff_human_in_loop(
                    agent,
                    logger,
                    state,
                    reason="The agent could not finish the form on its own.",
                    hint="Fill any missing fields and submit (or click Next) yourself. "
                         "When you're done, press ENTER and the agent will resume from "
                         "the page you're currently on.",
                )
                if self._handoff_skipped_job(
                    handoff, agent, logger, ExecutionPhase.HUMAN
                ):
                    self._check_stop()
                    self.job_repo.update_status(job.id or 0, ApplicationStatus.SKIPPED)
                    return ApplicationStatus.SKIPPED
                if handoff.cancelled:
                    self._check_stop() # Will raise if stopped
                    self.job_repo.update_status(job.id or 0, ApplicationStatus.FAILED)
                    return ApplicationStatus.FAILED
                page = handoff.page
                agent.page = page
                # LEARNING LOOP: Scrape whatever the human typed in the browser 
                # back into our persistent memory so we reuse it next time.
                try:
                    self._scrape_browser_state_to_memory(page, state, agent)
                except Exception:
                    pass
                # If the human advanced (e.g. clicked Next/Submit), give
                # the agent one more pass on the *new* page rather than
                # declaring success/failure based on the old one.
                if getattr(self, "_form_ready_for_submit", False):
                    success = self._submission_looks_plausible(page)
                if not success and handoff.advanced:
                    self._check_stop()
                    agent.show_status(
                        "Continuing from where you left off...",
                        phase=ExecutionPhase.HUMAN,
                    )
                    success = self._agent_fill_loop(
                        page, state, logger, agent, apply_was_clicked=True
                    )
                if not success:
                    success = self._submission_looks_plausible(page)
            self._check_stop()
            if getattr(self, "_job_skipped_by_user", False):
                self.job_repo.update_status(job.id or 0, ApplicationStatus.SKIPPED)
                return ApplicationStatus.SKIPPED
            # ── 5. Final status ─────────────────────────────────────
            if success:
                agent.show_success("Application completed successfully!")
                logger.info("Application completed successfully")
                self.job_repo.update_status(job.id or 0, ApplicationStatus.SUBMITTED)
                status = ApplicationStatus.SUBMITTED
                state.status = ApplicationStatus.SUBMITTED
            else:
                agent.show_error("Application could not be verified as submitted.")
                logger.error("Application failed")
                self.job_repo.update_status(job.id or 0, ApplicationStatus.FAILED)
                status = ApplicationStatus.FAILED
                state.status = ApplicationStatus.FAILED
                # Increment apps_since_sync for central DB sync
                from jobcli.storage.repositories import SyncMetadataRepository
                sync_repo = SyncMetadataRepository(self.session)
                sync_repo.increment_apps_since_sync()

            # ── 6. Final browser pause ──────────────────────────────
            # On failure: show the 2s countdown ("Moving to next job in 2s…")
            # so the human can glance at the browser before the agent moves on.
            # On success: if human manually filled any fields, give a 2s window
            # so they can see the confirmation page; otherwise a 1s flash is fine.
            if not self.config.headless:
                if not success:
                    agent.final_browser_pause()
                elif state.human_fields > 0:
                    # Human was involved — show a brief countdown before moving on
                    agent.final_browser_pause()
                else:
                    # Fully automated — just a quick 1s flash, keep the batch moving
                    page.wait_for_timeout(1000)

            # ── 7. Populate field-ownership counters on state ────────
            # These are read by cli/main.py → tracker.add_application()
            # to send real per-field counts to the backend.
            state.autofill_fields = getattr(self, "_last_extension_filled_count", 0)
            state.llm_fields = (
                getattr(self, "_last_rules_filled_count", 0)
                + getattr(self, "_last_llm_filled_count", 0)
            )
            state.human_fields = getattr(self, "_last_human_filled_count", 0)
            state.total_fields = state.autofill_fields + state.llm_fields + state.human_fields

            return status
        except (InterruptedError, KeyboardInterrupt):
            # Clean stop requested via CLI
            agent.show_warning("Application process stopped by user.")
            logger.warning("Application cancelled by user", phase=state.current_phase)
            self.job_repo.update_status(job.id or 0, ApplicationStatus.FAILED)
            return ApplicationStatus.FAILED
        except BrowserClosed:
            raise
        except Exception as e:
            logger.error(f"Application error: {e}")
            if self.config.screenshot_on_error:
                try:
                    logger.capture_screenshot(page, "error", state.current_phase)
                except Exception:
                    pass
            self.job_repo.update_status(job.id or 0, ApplicationStatus.FAILED)
            return ApplicationStatus.FAILED
        finally:
            # We NO LONGER close the page here. It is closed in stop_session().
            global_logger.info(f"Completed job {job.id}", status=state.status.value)

    def _click_apply_button(
        self,
        page: Page,
        state: ApplicationState,
        logger: JobLogger,
        ats_type: ATSType,
    ) -> tuple[bool, Page]:
        """Phase 1a: Click Apply and follow new tab / popup / redirect when needed."""
        try:
            context = page.context
            page_ids_before = {id(p) for p in context.pages}
            page_count_before = len(context.pages)
            url_before = page.url

            handler = ATSHandlerFactory.create_handler(ats_type, page, self.resume, logger)
            if handler:
                self._set_workday_modal_resume_path(handler)
                logger.info(f"Using {ats_type.value} handler", phase=ExecutionPhase.RULES)
                ok = handler.find_apply_button()
                page = adopt_application_page_after_action(
                    page,
                    page_count_before=page_count_before,
                    url_before=url_before,
                    logger=logger,
                    page_ids_before=page_ids_before,
                )
                if ok:
                    self._random_delay()
                    logger.capture_screenshot(page, "after_apply_click", ExecutionPhase.RULES)
                return ok, page

            apply_locator = ApplyButtonLocator(page, logger)
            ok, page = apply_locator.click_apply_button()
            if ok:
                self._random_delay()
                logger.capture_screenshot(page, "after_apply_click", ExecutionPhase.RULES)
            return ok, page
        except Exception as e:
            logger.error(f"Apply click failed: {e}", phase=ExecutionPhase.RULES)
        return False, page

    def _submission_looks_plausible(self, page: Page) -> bool:
        """Heuristic: URL or page text suggests a completed application (not just a click)."""
        try:
            url = (page.url or "").lower()
        except Exception:
            return False
        if any(
            kw in url
            for kw in (
                "thank",
                "success",
                "confirm",
                "submitted",
                "complete",
                "received",
                "acknowledgement",
            )
        ):
            return True
        try:
            blob = (page.content() or "")[:120000].lower()
        except Exception:
            return False

        for pat in (
            r"thank you for applying",
            r"application received",
            r"successfully submitted",
            r"submission.{0,40}complete",
            r"we.{0,60}received your application",
            r"your application has been submitted",
        ):
            if re.search(pat, blob, re.I):
                return True
        return False

    def _handoff_indicates_submitted(
        self, page: Page, handoff: "HandoffResult"
    ) -> bool:
        """True when the human already submitted during a mid-fill handoff."""
        if getattr(handoff, "submitted", False):
            return True
        if self._submission_looks_plausible(page):
            return True
        title = (handoff.title_after or "").lower()
        if any(
            phrase in title
            for phrase in (
                "thank you for applying",
                "thank you for your application",
                "application received",
                "application submitted",
                "successfully submitted",
            )
        ):
            return True
        try:
            strong, soft, _signals = self._looks_like_confirmation(
                page,
                handoff.url_before or "",
                pre_submit_had_submit_btn=True,
            )
            if strong or soft:
                return True
        except Exception:
            pass
        return False

    def _fill_form_rules(
        self,
        page: Page,
        state: ApplicationState,
        logger: JobLogger,
        ats_type: ATSType,
    ) -> bool:
        """Phase 1b: Fill form with rule-based locators."""
        logger.log_phase_start(ExecutionPhase.RULES)
        state.current_phase = ExecutionPhase.RULES
        try:
            logger.info("Starting form fill", phase=ExecutionPhase.RULES)
            resume_path = self.config.resume_pdf_path

            handler = ATSHandlerFactory.create_handler(ats_type, page, self.resume, logger)
            if handler:
                self._set_workday_modal_resume_path(handler)
                handler.fill_form(resume_path)
                self._random_delay()
                # Same as legacy _phase_rules: wizard flows need Next/Continue before final submit.
                max_steps = 5
                for step in range(max_steps):
                    state.step_count = step + 1
                    if not handler.handle_multi_step(state):
                        break
                    self._random_delay()
                clicked = handler.submit_application()
                if not clicked:
                    logger.log_phase_end(ExecutionPhase.RULES, False)
                    return False
                page.wait_for_timeout(2500)
                success = self._submission_looks_plausible(page)
                if not success:
                    logger.warning(
                        "A submit-style control was clicked, but no thank-you / confirmation "
                        "signal was detected. The application may still be in progress.",
                        phase=ExecutionPhase.RULES,
                    )
                logger.log_phase_end(ExecutionPhase.RULES, success)
                return success

            form_filler = FormFiller(page, self.resume, logger)
            fill_results = form_filler.fill_all(resume_path)

            personal_results = fill_results.get("personal_info", {})
            fields_filled = sum(1 for v in personal_results.values() if v)
            resume_uploaded = fill_results.get("resume_uploaded", False)

            self._random_delay()
            logger.capture_screenshot(page, "form_filled", ExecutionPhase.RULES)

            if fields_filled > 0 or resume_uploaded:
                logger.info(
                    f"Form fill validated: {fields_filled} fields filled",
                    phase=ExecutionPhase.RULES,
                )
                logger.log_phase_end(ExecutionPhase.RULES, True)
                return True
            else:
                logger.warning("0 fields filled by rules. Falling through to LLM.", phase=ExecutionPhase.RULES)
                logger.log_phase_end(ExecutionPhase.RULES, False)
                return False
        except (InterruptedError, KeyboardInterrupt):
            raise
        except Exception as e:
            logger.error(f"Form fill failed: {e}", phase=ExecutionPhase.RULES)
            logger.log_phase_end(ExecutionPhase.RULES, False)
            return False

    def _phase_rules(
        self,
        page: Page,
        state: ApplicationState,
        logger: JobLogger,
        ats_type: ATSType,
    ) -> bool:
        """Phase 1 (combined): Legacy path for ATS handlers with multi-step support."""
        logger.log_phase_start(ExecutionPhase.RULES)
        state.current_phase = ExecutionPhase.RULES
        try:
            handler = ATSHandlerFactory.create_handler(ats_type, page, self.resume, logger)
            if handler:
                self._set_workday_modal_resume_path(handler)
                logger.info(f"Using {ats_type.value} handler", phase=ExecutionPhase.RULES)
                if not handler.find_apply_button():
                    logger.warning("ATS handler failed to find apply button")
                    return False
                self._random_delay()
                logger.capture_screenshot(page, "after_apply_click", ExecutionPhase.RULES)
                resume_path = self.config.resume_pdf_path
                handler.fill_form(resume_path)
                self._random_delay()
                logger.capture_screenshot(page, "form_filled", ExecutionPhase.RULES)
                max_steps = 5
                for step in range(max_steps):
                    state.step_count = step + 1
                    if not handler.handle_multi_step(state):
                        break
                    self._random_delay()
                success = handler.submit_application()
                logger.log_phase_end(ExecutionPhase.RULES, success)
                return success
        except Exception as e:
            logger.error(f"Phase 1 failed: {e}", phase=ExecutionPhase.RULES)
            logger.log_phase_end(ExecutionPhase.RULES, False)
        return False

    def _run_rules_prefill(
        self,
        rules_handler: Any,
        page: Page,
        logger: JobLogger,
        agent: "AgentInterface",
        extractor: "AccessibilityTreeExtractor",
        state: "ApplicationState",
    ) -> Dict[str, bool]:
        """Run the ATS-specific rule-based fill, then a generic confidence-scored
        fill for anything the ATS handler couldn't resolve.

        Returns a merged ``{short_key: filled?}`` dict. Each key represents a
        canonical personal field (``first_name``, ``last_name``, ``email``,
        ``phone``, ``linkedin``, …). Cheap, deterministic, and re-runnable
        before AND after the resume upload — which is the seam where most
        forms reveal additional fields.

        Runs after extension autofill and before the LLM. Also used as a
        fallback when the LLM makes no progress and for resume-upload helpers.
        Fill order: extension → rules → LLM.
        """
        merged: Dict[str, bool] = {}

        # 1) ATS-handler narrow selectors (Ashby uses input[name='firstName']
        #    etc., Greenhouse uses id contains, Workday uses data-automation-id,
        #    Lever uses name attribute). These succeed in O(ms) when present.
        try:
            ats_filled = rules_handler.fill_form(self.config.resume_pdf_path) or {}
            for k, v in ats_filled.items():
                if v:
                    merged[k] = True
        except Exception as e:
            logger.warning(
                f"ATS-specific fill_form raised {e!r}; continuing with generic pass.",
                phase=ExecutionPhase.RULES,
            )

        # 2) Generic confidence-scored fill for whatever wasn't filled yet.
        #    Uses FieldConfidenceScorer with the FIELD_KEYWORDS map ported
        #    from the extension's ``genericStrategy.js``, so it catches
        #    label/aria/name/placeholder variants the ATS handler doesn't
        #    hard-code. Critically, this also runs even when the detected
        #    ATS is UNKNOWN.
        try:
            from jobcli.ats.locators.form_fields import FormFiller

            generic_filler = FormFiller(page, self.resume, logger)
            generic_results = generic_filler.fill_personal_info() or {}
            for k, v in generic_results.items():
                if v and k not in merged:
                    merged[k] = True
        except Exception as e:
            logger.warning(
                f"Generic confidence fill raised {e!r}; continuing.",
                phase=ExecutionPhase.RULES,
            )

        filled = [k for k, v in merged.items() if v]
        if filled:
            agent.show_success(
                f"Rules filled {len(filled)} field(s): " + ", ".join(filled)
            )
            logger.info(
                f"ATS pre-pass filled {len(filled)} fields: {filled}",
                phase=ExecutionPhase.RULES,
            )
            # Let React state settle before we re-read the DOM.
            page.wait_for_timeout(500)
        else:
            logger.info(
                "ATS pre-pass produced no fills — form may use unusual field "
                "names; LLM will handle it.",
                phase=ExecutionPhase.RULES,
            )

        return merged

    def _get_extension_service_worker(self, page: Page, logger: JobLogger, timeout_ms: int = 5000):
        """Return the TalentScreen extension's MV3 service worker, waking it if necessary.

        MV3 workers are lazy: they don't appear in ``context.service_workers``
        until *something* triggers them (an event, a message, or first
        ``chrome.runtime`` use from a content script on a host they match).
        If we naively call ``context.service_workers[0]``, the list is empty
        right after browser launch and storage injection silently no-ops —
        which is one of the two root causes of "extension doesn't autofill".

        We poll up to ``timeout_ms`` for a worker to appear (Chrome wakes it
        automatically once a matching page navigates). Returns ``None`` if it
        never wakes (extension not loaded, wrong manifest, etc.) — caller
        treats that as "extension unavailable, rely on the Python rules
        pre-pass instead".
        """
        context = page.context
        # Fast path
        if context.service_workers:
            return context.service_workers[0]
        if context.background_pages:
            return context.background_pages[0]
        # Poll – the worker typically wakes within a few hundred ms of the
        # first content-script-matching navigation.
        import time as _time
        deadline = _time.monotonic() + (timeout_ms / 1000.0)
        while _time.monotonic() < deadline:
            try:
                page.wait_for_timeout(150)
            except Exception:
                _time.sleep(0.15)
            if context.service_workers:
                return context.service_workers[0]
            if context.background_pages:
                return context.background_pages[0]
        logger.debug(
            f"No extension service worker after {timeout_ms}ms wait — "
            "the bundled TalentScreen extension may not be loaded. Rules "
            "pre-pass will still fill the deterministic fields."
        )
        return None

    def _ensure_extension_payloads(self, logger: JobLogger) -> bool:
        """Load JSON Resume + PDF blob from DB once per apply run."""
        if self._extension_resume_data is not None:
            return True
        try:
            from jobcli.utils.extension_resume import (
                ExtensionResumeError,
                load_extension_payloads,
            )

            resume_data, resume_file = load_extension_payloads(self.session)
            self._extension_resume_data = resume_data
            self._extension_resume_file = resume_file
            return True
        except ExtensionResumeError as e:
            logger.warning(
                f"Extension autofill skipped — {e}",
                phase=ExecutionPhase.RULES,
            )
            return False

    def _inject_resume_into_extension(
        self,
        page: Page,
        logger: JobLogger,
        *,
        trigger_fill: bool = True,
    ) -> None:
        """Inject JSON Resume + normalized index + PDF into extension storage.

        Mirrors the side panel: ``resumeData`` (raw JSON Resume),
        ``normalizedData`` (``ResumeProcessor.normalize`` in the service worker),
        and ``resumeFile`` (PDF data URL). Then optionally dispatches
        ``fill_form`` with the normalized payload.
        """
        try:
            if not self.resume:
                return

            if not self._ensure_extension_payloads(logger):
                return

            bg_target = self._get_extension_service_worker(page, logger)
            if not bg_target:
                return

            resume_data = self._extension_resume_data
            resume_file = self._extension_resume_file

            inject_js = """
                async ({ resumeData, resumeFile }) => {
                    let normalizedData;
                    try {
                        normalizedData = ResumeProcessor.normalize(resumeData);
                    } catch (e) {
                        return {
                            ok: false,
                            reason: 'normalize_failed',
                            error: String(e),
                        };
                    }
                    await new Promise((resolve) => chrome.storage.local.set({
                        resumeData,
                        normalizedData,
                        resumeFile: resumeFile || null,
                        autoTriggerEnabled: true,
                        autoRunActive: true,
                        currentJobIndex: 0,
                        totalJobs: 1,
                    }, resolve));
                    const verify = await new Promise((resolve) =>
                        chrome.storage.local.get(
                            ['resumeData', 'normalizedData', 'resumeFile', 'autoRunActive'],
                            resolve
                        )
                    );
                    const nd = verify.normalizedData || {};
                    const id = nd.identity || {};
                    const contact = nd.contact || {};
                    return {
                        ok: true,
                        normalizedData: nd,
                        resumeFile: verify.resumeFile || null,
                        wrote_resume_data: !!verify.resumeData,
                        wrote_normalized: !!(
                            id.first_name || contact.email || nd.employment
                        ),
                        wrote_pdf: !!(verify.resumeFile && verify.resumeFile.data),
                        autoRunActive: verify.autoRunActive === true,
                    };
                }
            """
            try:
                result = bg_target.evaluate(
                    inject_js,
                    {"resumeData": resume_data, "resumeFile": resume_file},
                )
            except Exception as e:
                logger.debug(f"Service-worker evaluate failed during injection: {e}")
                return

            if not result or not result.get("ok"):
                logger.debug(
                    f"Extension resume injection failed: {result}",
                    phase=ExecutionPhase.RULES,
                )
                return

            self._extension_normalized_data = result.get("normalizedData")

            if not result.get("wrote_normalized"):
                logger.debug(
                    "Extension normalize produced empty identity/contact — "
                    "check JSON Resume basics.",
                    phase=ExecutionPhase.RULES,
                )
                return

            parts = ["resumeData", "normalizedData"]
            if result.get("wrote_pdf"):
                parts.append("resumeFile")
            logger.info(
                "Successfully injected resume into extension storage "
                f"({', '.join(parts)})."
            )

            if trigger_fill:
                self._send_extension_fill_message(
                    bg_target,
                    self._extension_normalized_data,
                    self._extension_resume_file,
                    logger,
                )

        except Exception as e:
            logger.error(f"Failed to inject resume into extension: {e}")

    def _send_extension_fill_message(
        self,
        bg_target: Any,
        normalized_data: Optional[dict],
        resume_file: Optional[dict],
        logger: JobLogger,
    ) -> None:
        """Ask the active Playwright tab's content script to run ``fillForm``."""
        if not normalized_data:
            logger.debug(
                "Extension fill_form skipped — no normalizedData.",
                phase=ExecutionPhase.RULES,
            )
            return
        try:
            poke_js = """
                async ({ normalizedData, resumeFile }) => {
                    const tabs = await new Promise(r =>
                        chrome.tabs.query({ active: true, currentWindow: true }, r)
                    );
                    const tab = tabs && tabs[0];
                    if (!tab || !tab.id) return { ok: false, reason: 'no_active_tab' };
                    let err = null;
                    await new Promise((resolve) => {
                        chrome.tabs.sendMessage(
                            tab.id,
                            {
                                action: 'fill_form',
                                normalizedData,
                                resumeFile: resumeFile || null,
                                manualEdits: {},
                            },
                            () => {
                                err = chrome.runtime.lastError
                                    ? chrome.runtime.lastError.message
                                    : null;
                                resolve();
                            }
                        );
                    });
                    return {
                        ok: !err,
                        tabId: tab.id,
                        url: tab.url || '',
                        error: err,
                        hasPdf: !!(resumeFile && resumeFile.data),
                    };
                }
            """
            result = bg_target.evaluate(
                poke_js,
                {"normalizedData": normalized_data, "resumeFile": resume_file},
            )
            if result and result.get("ok"):
                pdf_note = " with PDF" if result.get("hasPdf") else ""
                logger.info(
                    "Extension fill_form dispatched to active tab "
                    f"(tab {result.get('tabId')}){pdf_note}",
                    phase=ExecutionPhase.RULES,
                )
            else:
                logger.debug(
                    f"Extension fill_form on active tab did not confirm: {result}",
                    phase=ExecutionPhase.RULES,
                )
        except Exception as e:
            logger.debug(
                f"Extension fill_form dispatch failed: {e}",
                phase=ExecutionPhase.RULES,
            )

    # ── Don't-refill snapshot ────────────────────────────────────────────
    # The TalentScreen extension, the deterministic rule pass, and the LLM
    # iteration loop all run against the same page. Without a cross-pass
    # "this field already has a value" view they keep stepping on each
    # other's toes — the user sees the same field flicker filled twice
    # or three times. ``_snapshot_filled`` is the single source of truth:
    # call it after a settle wait, then again at the top of every LLM
    # iteration. Any FILL / TYPE / SELECT action whose target appears in
    # the snapshot is dropped before it reaches the executor.
    # ────────────────────────────────────────────────────────────────────

    # Tunable: how long to wait after dispatching extension fill_form before
    # snapshotting the DOM and handing off to the LLM.
    EXTENSION_AUTOFILL_SETTLE_MS = 3500

    _SNAPSHOT_PLACEHOLDERS = tuple(PLACEHOLDER_VALUES)

    def _snapshot_filled(self, page: Page) -> dict[str, str]:
        """Return ``{normalized_key: current_value}`` for every form input
        that currently has a non-empty, non-placeholder value.

        The key set is intentionally wide (name, id, aria-label, placeholder)
        so a later FILL action proposed against ANY of those identifiers
        can be deduped without rebuilding the snapshot.
        """
        try:
            raw = page.evaluate(
                """() => {
                    const out = [];
                    document.querySelectorAll(
                        'input, select, textarea, [contenteditable="true"], [role="combobox"]'
                    ).forEach(el => {
                        let val = '';
                        if (el.tagName === 'SELECT' && el.selectedIndex >= 0) {
                            val = el.options[el.selectedIndex].text || '';
                        } else if (el.isContentEditable) {
                            val = el.innerText || el.textContent || '';
                        } else if (el.getAttribute('role') === 'combobox') {
                            val = el.innerText || el.textContent || el.value || '';
                        } else {
                            val = el.value || '';
                        }
                        if (!val || !val.trim()) return;
                        const v = val.trim();
                        const keys = [];
                        if (el.name) keys.push('name:' + el.name.toLowerCase());
                        if (el.id)   keys.push('id:'   + el.id.toLowerCase());
                        const al = el.getAttribute('aria-label');
                        if (al) keys.push('label:' + al.toLowerCase());
                        if (el.placeholder) keys.push('ph:' + el.placeholder.toLowerCase());
                        if (keys.length) out.push({keys, value: v});
                    });
                    return out;
                }"""
            )
        except Exception:
            return {}

        snap: dict[str, str] = {}
        for row in raw or []:
            v = (row.get("value") or "").strip()
            if not is_meaningful_value(v):
                continue
            for k in row.get("keys", []) or []:
                if k:
                    snap[k] = v
        return snap

    @staticmethod
    def _snapshot_field_idents(snapshot: dict[str, str]) -> set[str]:
        """Unique field identifiers from a :meth:`_snapshot_filled` dict."""
        idents: set[str] = set()
        for key in snapshot:
            _, _, ident = key.partition(":")
            if ident:
                idents.add(ident)
        return idents

    def _build_prefilled_field_lines(
        self,
        prefilled_snapshot: dict[str, str],
        ax_tree: Any,
    ) -> list[str]:
        """Human-readable lines for the LLM: fields that must not be re-filled."""
        lines: list[str] = []
        seen_pairs: set[tuple[str, str]] = set()
        for key, val in prefilled_snapshot.items():
            _, _, ident = key.partition(":")
            if not ident or not is_meaningful_value(val):
                continue
            pair = (ident, val.lower())
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            lines.append(f"- {ident}: already has value '{val}'")
        for field in getattr(ax_tree, "form_fields", []) or []:
            val = str(field.get("value", "")).strip()
            label = field.get("name") or field.get("label") or "unknown"
            if is_meaningful_value(val):
                entry = f"- {label}: already has value '{val}'"
                if entry not in lines:
                    lines.append(entry)
        return lines

    # Application-fill order (do not reorder without updating docs/CLAUDE.md):
    #   1. Chrome extension autofill — primary fill
    #   2. Rules-based ATS handlers — FALLBACK, runs only when (1) filled 0
    #      visible fields (covers both "extension not loaded" and "extension
    #      ran but produced nothing"). Gated by self._last_extension_filled_count.
    #   3. LLM agent loop — fills whatever (1)/(2) left blank.
    #   4. Human-in-the-loop review — COMPULSORY in all modes including
    #      AUTO. Invoked with force_block=True in _handoff_human_in_loop, so
    #      the AUTO short-circuit in agent_interface.handoff_to_human is
    #      bypassed. Submit click is skipped when _looks_like_confirmation
    #      detects the human already submitted in the browser.
    FILL_PIPELINE_STEPS = (
        "① Extension",
        "② Rules (fallback)",
        "③ LLM",
        "④ Human review (always)",
    )

    def _log_fill_pipeline_start(self, logger: JobLogger, agent: "AgentInterface") -> None:
        pipeline = " → ".join(self.FILL_PIPELINE_STEPS)
        logger.info(f"Fill pipeline: {pipeline}", phase=ExecutionPhase.RULES)
        agent.show_status(
            f"Pipeline: {pipeline}",
            phase=ExecutionPhase.RULES,
        )

    def _handoff_skipped_job(
        self,
        handoff: "HandoffResult",
        agent: "AgentInterface",
        logger: JobLogger,
        phase: ExecutionPhase,
    ) -> bool:
        """Return True if the human typed skip at a handoff (skip this job)."""
        if not getattr(handoff, "skipped", False):
            return False
        self._job_skipped_by_user = True
        logger.info("Application skipped by user at handoff", phase=phase)
        return True

    def _handoff_human_in_loop(
        self,
        agent: "AgentInterface",
        logger: JobLogger,
        state: ApplicationState,
        reason: str,
        hint: str,
        *,
        force_block: bool = False,
        submission_checker: Optional[Any] = None,
    ) -> "HandoffResult":
        """Phase 4 — human takes the browser after extension, rules, and LLM.

        Set ``force_block=True`` for the compulsory pre-submit review so the
        handoff blocks in AUTO mode too (rules ran only if extension produced
        no fills; the human review is non-negotiable).
        """
        state.current_phase = ExecutionPhase.HUMAN
        logger.log_phase_start(ExecutionPhase.HUMAN)
        logger.info(
            "Phase 4/4: Human review before submit "
            "(extension and LLM already ran; rules ran only if extension produced no fills)",
            phase=ExecutionPhase.HUMAN,
        )
        agent.show_phase_banner("Human review (4/4)")

        # ── Snapshot fields BEFORE the human takes over ───────────────
        try:
            _before_snap = self._snapshot_filled(agent.page)
            _before_idents = self._snapshot_field_idents(_before_snap)
        except Exception:
            _before_idents = set()

        result = agent.handoff_to_human(
            reason=reason,
            hint=hint,
            force_block=force_block,
            submission_checker=submission_checker,
        )

        # ── Snapshot fields AFTER the human is done — diff = human fills ─
        if not result.cancelled:
            try:
                _after_snap = self._snapshot_filled(result.page)
                _after_idents = self._snapshot_field_idents(_after_snap)
                _human_new = len(_after_idents - _before_idents)
                self._last_human_filled_count = (
                    getattr(self, "_last_human_filled_count", 0) + _human_new
                )
                if _human_new:
                    logger.info(
                        f"Human filled {_human_new} new field(s) during handoff.",
                        phase=ExecutionPhase.HUMAN,
                    )
            except Exception:
                pass

        logger.log_phase_end(ExecutionPhase.HUMAN, not result.cancelled)
        return result

    # ── Confirmation-page detection ──────────────────────────────────────
    # The user may submit the form themselves during the compulsory
    # pre-submit review handoff. In that case the agent must skip its own
    # submit click — otherwise it either clicks a now-disabled button or
    # falls into the "couldn't find submit" fallback handoff. We reuse the
    # same four-signal detector that runs after the agent's own submit
    # click, so behaviour is identical regardless of who pressed Submit.
    _CONFIRMATION_TEXTS = (
        # Generic
        "thank you", "thanks!", "thanks for applying",
        "application submitted", "successfully submitted",
        "application received", "application is received",
        "we have received your application",
        "we've received your application",
        "application sent", "application complete",
        "your application has been submitted",
        "you've applied", "you have applied",
        # Ashby phrasing
        "we'll be in touch", "we will be in touch",
        # Greenhouse / Lever phrasing
        "thank you for your interest", "thanks for your interest",
        "application confirmed",
    )
    _CONFIRMATION_URL_TERMS = (
        "success", "confirmation", "confirm",
        "thank-you", "thank_you", "thanks",
        "submitted", "application-confirmation",
        "complete", "completed", "applied",
    )

    def _looks_like_confirmation(
        self,
        page: Page,
        pre_submit_url: str,
        pre_submit_had_submit_btn: bool,
    ) -> tuple[bool, bool, dict]:
        """Return ``(strong, soft, signals)`` indicating whether *page* looks
        like a post-submit confirmation."""
        from jobcli.utils.form_sync import looks_like_confirmation

        return looks_like_confirmation(
            page, pre_submit_url, pre_submit_had_submit_btn
        )

    def _run_extension_autofill_phase(
        self,
        page: Page,
        logger: JobLogger,
        agent: "AgentInterface",
    ) -> dict[str, str]:
        """Phase 1/4 — TalentScreen Chrome extension autofill (primary fill).

        Injects resume → ``fill_form`` on active tab → settle wait → snapshot.
        """
        agent.show_phase_banner("Chrome extension autofill (1/4: primary fill)")
        if not self.extension_dir:
            logger.warning(
                "Extension not loaded — skipping extension autofill; LLM only.",
                phase=ExecutionPhase.RULES,
            )
            return self._snapshot_filled(page)

        agent.show_status(
            "Running TalentScreen extension autofill…",
            phase=ExecutionPhase.RULES,
        )
        before = self._snapshot_filled(page)
        before_idents = self._snapshot_field_idents(before)

        self._inject_resume_into_extension(page, logger, trigger_fill=True)
        page.wait_for_timeout(self.EXTENSION_AUTOFILL_SETTLE_MS)

        after = self._snapshot_filled(page)
        new_idents = self._snapshot_field_idents(after) - before_idents
        self._last_extension_filled_count = len(new_idents)

        if new_idents:
            sample = ", ".join(sorted(new_idents)[:10])
            if len(new_idents) > 10:
                sample += ", …"
            agent.show_success(
                f"Extension filled {len(new_idents)} field(s): {sample}"
            )
            logger.info(
                f"Extension autofill populated {len(new_idents)} field(s): "
                f"{sorted(new_idents)}",
                phase=ExecutionPhase.RULES,
            )
        else:
            logger.info(
                "Extension autofill produced no new visible fields — LLM will continue.",
                phase=ExecutionPhase.RULES,
            )

        return after

    def _action_target_already_filled(
        self,
        action: BrowserAction,
        snapshot: dict[str, str],
    ) -> Optional[str]:
        """Return the current live value if ``action``'s target is already
        populated (per ``snapshot``), else ``None``.

        Normalizes both sides by stripping required-markers (``*``) so
        ``"Country*"`` (LLM) correctly matches ``"country"`` (extension
        snapshot), while only returning a hit when the snapshot value is
        non-empty (i.e., the field was actually filled).
        """
        import re as _re

        def _norm(s: str) -> str:
            return _re.sub(r"\s+", " ", _re.sub(r"\*+", "", s or "").strip()).lower()

        if action.action not in (ActionType.FILL, ActionType.TYPE, ActionType.SELECT):
            return None
        sel = _norm(action.selector or "")
        lbl = _norm(action.field_label or "")
        if not sel and not lbl:
            return None
        for key, val in snapshot.items():
            _, _, suffix = key.partition(":")
            if not suffix:
                continue
            # Exact match first (most reliable)
            if suffix == sel or suffix == lbl:
                return val
            # Fuzzy only when suffix is >= 8 chars (avoids false matches on short tokens like "name")
            if len(suffix) >= 8:
                if (sel and suffix in sel) or (lbl and suffix in lbl):
                    return val
        return None

    def _prepare_gaps_for_llm(
        self,
        ax_tree,
        page: Page,
        memory: "AgentMemory",
        synonym_resolver,
        state: ApplicationState,
    ) -> tuple[list[dict[str, Any]], str]:
        """Build enriched gap list and gap-hint block for the LLM auditor."""
        prefiller = MemoryPrefiller(synonym_resolver)
        gaps = build_empty_fields_payload(
            ax_tree,
            dropdown_options=getattr(ax_tree, "dropdown_fields", None),
            extra_gap_labels=_unselected_required_dropdowns(page),
        )
        enriched = prefiller.enrich_gaps_with_suggestions(
            gaps,
            memory,
            self.resume,
            state.detected_ats,
            self.common_questions,
        )
        gap_hints = memory.build_gap_hints(
            enriched,
            self.resume,
            state.detected_ats,
            common_questions=self.common_questions,
        )
        return enriched, gap_hints

    def _run_memory_prefill(
        self,
        page: Page,
        ax_tree,
        extractor: AccessibilityTreeExtractor,
        executor: ToolExecutor,
        memory: "AgentMemory",
        synonym_resolver,
        state: ApplicationState,
        logger: JobLogger,
        agent: AgentInterface,
    ) -> tuple[Any, list[dict[str, Any]]]:
        """Phase 2b — DISABLED.  Previously filled gaps from resume JSON + DB memory.

        Autonomous memory-based fill execution has been disabled because:
          - Label matching is approximate and fills wrong fields
          - Confidence gate (0.6) is too low, promoting bad answers
          - Wrong fills pre-empt the LLM and are never corrected
            (the don't-refill snapshot filter skips already-filled fields)

        The LLM still receives memory as *context* via ``_prepare_gaps_for_llm``
        (``enriched_gaps`` with ``suggested_value`` hints and ``gap_hints``).
        This method is a no-op kept for backwards-compat with callers outside
        ``_agent_fill_loop`` (none currently exist).
        """
        from jobcli.intelligence.memory_prefill import MEMORY_PREFILL_DISABLED
        if MEMORY_PREFILL_DISABLED:
            # Return the unchanged ax_tree + enriched gaps (hints only, no actions).
            enriched, gap_hints = self._prepare_gaps_for_llm(
                ax_tree, page, memory, synonym_resolver, state
            )
            return ax_tree, enriched

        # ── Legacy path (only reachable when MEMORY_PREFILL_DISABLED=False) ──
        enriched, _ = self._prepare_gaps_for_llm(
            ax_tree, page, memory, synonym_resolver, state
        )
        # Disabled memory prefill execution as it sometimes fills incorrect information
        return ax_tree, enriched

    def _agent_fill_loop(
        self,
        page: Page,
        state: ApplicationState,
        logger: JobLogger,
        agent: AgentInterface,
        apply_was_clicked: bool = False,
        resume_pdf_path: Optional[str] = None,
    ) -> bool:
        """Application fill: extension → rules → LLM → human-in-the-loop (last).

        Phases 1–3 run in a fixed order before any LLM iteration. Phase 4
        (``_handoff_human_in_loop`` / ``show_failed_fields``) runs only when
        automation cannot finish or ``InteractionMode`` requires review.
        """
        # LLM is globally disabled — skip the API-key check entirely and
        # fall straight through to Extension + Rules fill.
        llm_client = self._get_llm_client(logger)
        provider = self.config.default_llm_provider or "default"
        try:
            page.wait_for_timeout(500)
            self._dismiss_cookie_consent(page, logger)

            if apply_was_clicked:
                try:
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(300)
                except Exception:
                    pass

            self._log_fill_pipeline_start(logger, agent)

            # ── Phase 1/4: TalentScreen extension autofill ────────────────
            self._last_extension_filled_count = 0
            self._last_llm_filled_count = 0
            self._last_human_filled_count = 0
            prefilled_snapshot = self._run_extension_autofill_phase(page, logger, agent)

            # ── Phase 2/4: Rules-based prefill (FALLBACK ONLY) ────────────
            # Rules are a deterministic safety net for cases where the
            # extension was unable to fill the form. When the extension
            # already populated >=1 visible field we skip rules entirely
            # and go straight to the LLM (and then to the compulsory
            # human review). See _last_extension_filled_count in
            # _run_extension_autofill_phase.
            # LLM is disabled globally — always use Extension + Rules + Human only.
            skip_llm = True

            rules_handler = None
            self._last_rules_filled_count = 0
            try:
                rules_handler = ATSHandlerFactory.create_handler(
                    state.detected_ats, page, self.resume, logger
                )
                if skip_llm or state.detected_ats == ATSType.ASHBY:
                    agent.show_phase_banner(f"Hybrid Extension + Playwright Automation ({state.detected_ats.value} - No LLM)")
                    if rules_handler:
                        pdf_to_use = resume_pdf_path or getattr(self, "resume_pdf_path", None)
                        if not pdf_to_use:
                            try:
                                from jobcli.storage.repositories import UserRepository
                                user_repo = UserRepository(self.session)
                                user_data = user_repo.get_user_data()
                                if user_data and getattr(user_data, "pdf_path", None):
                                    pdf_to_use = user_data.pdf_path
                            except Exception:
                                pass
                        rules_handler.fill_form(pdf_to_use)

                    # ── Human review gate ───────────────────────────────────
                    # Pause here so the human can check every field, correct
                    # mismatches, and fill anything the rules left blank.
                    # Only fires after ENTER + 2-second countdown.
                    agent.pause_for_form_review()

                    # ── Submit automatically with validation-error retry loop ──
                    if rules_handler:
                        max_submit_attempts = 5
                        for attempt in range(1, max_submit_attempts + 1):
                            submitted = rules_handler.submit_application()
                            if submitted:
                                agent.show_success(
                                    f"{state.detected_ats.value} application submitted "
                                    "via Extension + Rules."
                                )
                                logger.info(
                                    f"{state.detected_ats.value} application completed "
                                    "via Extension + Playwright",
                                    phase=ExecutionPhase.RULES,
                                )
                                break
                            else:
                                # Validation errors still on screen — pause for human fix
                                from rich.panel import Panel
                                agent.console.print(
                                    Panel(
                                        f"[bold red]Validation errors detected "
                                        f"(attempt {attempt}/{max_submit_attempts}).[/bold red]\n\n"
                                        "  [yellow]→[/yellow] Fix the highlighted fields in the browser\n"
                                        "  [yellow]→[/yellow] Then press [bold green]ENTER[/bold green] "
                                        "to retry submission",
                                        title="[bold red]>>> VALIDATION ERRORS — FIX & RETRY <<<[/bold red]",
                                        border_style="red",
                                    )
                                )
                                try:
                                    agent._get_user_input(
                                        "  [yellow]Press ENTER to retry submit →[/yellow] ",
                                        timeout_seconds=None,
                                        default="",
                                    )
                                except Exception:
                                    pass
                    return True

                if self._last_extension_filled_count == 0:
                    if rules_handler and state.detected_ats not in (ATSType.UNKNOWN,):
                        agent.show_phase_banner("Rules-based fallback (2/4: extension filled nothing)")
                        self._set_workday_modal_resume_path(rules_handler)
                        agent.show_status(
                            f"Extension filled nothing — running {state.detected_ats.value} rules as fallback…",
                            phase=ExecutionPhase.RULES,
                        )
                        prefill_results = self._run_rules_prefill(
                            rules_handler, page, logger, agent,
                            AccessibilityTreeExtractor(page), state,
                        )
                        filled = [k for k, v in (prefill_results or {}).items() if v]
                        self._last_rules_filled_count = len(filled)
                        if filled:
                            prefilled_snapshot = self._snapshot_filled(page)
                            page.wait_for_timeout(500)
                else:
                    logger.info(
                        f"Extension filled {self._last_extension_filled_count} field(s) — "
                        "skipping rules fallback, going straight to LLM.",
                        phase=ExecutionPhase.RULES,
                    )
                    agent.show_status(
                        f"Extension filled {self._last_extension_filled_count} field(s) — "
                        "skipping rules, going straight to LLM.",
                        phase=ExecutionPhase.RULES,
                    )
            except Exception as e:
                logger.debug(
                    f"Rules prefill after extension raised {e!r}",
                    phase=ExecutionPhase.RULES,
                )

            extractor = AccessibilityTreeExtractor(page)
            ax_tree = extractor.extract()
            logger.save_structured_dom(
                ax_tree.model_dump(), "ax_tree_after_extension_and_rules", ExecutionPhase.LLM
            )

            # ── AUTH / LOGIN GATE DETECTION ───────────────────────────────
            # Before letting the LLM touch the form, verify we are NOT on a
            # "Create account or Sign in" wall.  Workday / Oracle HCM /
            # iCIMS frequently gate the real application behind such a
            # screen, and if we auto-fill it the LLM will invent a
            # password + register a junk account under the user's real
            # email.  Up to 3 retries: human logs in, presses ENTER, we
            # re-extract and check again.  If we still see auth after
            # three rounds, we surface an error rather than silently
            # filling an auth form.
            for _auth_attempt in range(3):
                is_auth, auth_reason = _is_auth_form(page, ax_tree)
                if not is_auth:
                    break
                logger.warning(
                    f"Authentication gate detected: {auth_reason}",
                    phase=ExecutionPhase.HUMAN,
                )
                agent.show_warning(
                    "This page is asking for login / account-creation, not "
                    "an application form. Handing the browser to you so you "
                    "can sign in with your own credentials — the agent will "
                    "NEVER invent a password for you."
                )
                handoff = agent.handoff_to_human(
                    reason=(
                        "Login / sign-up / create-account screen detected. "
                        f"{auth_reason} "
                        "The agent will not auto-fill authentication forms — "
                        "please sign in (or create an account) yourself."
                    ),
                    hint=(
                        "Sign in or create an account in the browser, navigate "
                        "to the actual application form, then press ENTER so "
                        "the agent resumes from that page."
                    ),
                )
                if self._handoff_skipped_job(handoff, agent, logger, ExecutionPhase.LLM):
                    logger.log_phase_end(ExecutionPhase.LLM, False)
                    return False
                if handoff.cancelled:
                    logger.log_phase_end(ExecutionPhase.LLM, False)
                    return False
                page = handoff.page
                agent.page = page
                extractor = AccessibilityTreeExtractor(page)
                try:
                    ax_tree = extractor.extract()
                    logger.save_structured_dom(
                        ax_tree.model_dump(),
                        "ax_tree_after_auth_handoff",
                        ExecutionPhase.LLM,
                    )
                except Exception as e:
                    logger.error(
                        f"Could not re-extract AX tree after auth handoff: {e}",
                        phase=ExecutionPhase.LLM,
                    )
                    logger.log_phase_end(ExecutionPhase.LLM, False)
                    return False
            else:
                # ``for … else`` runs when the loop exits without ``break``
                # — i.e. we still see an auth form after three handoffs.
                agent.show_error(
                    "Still on a login / sign-up screen after three handoff "
                    "rounds. Stopping so we don't create a junk account."
                )
                logger.log_phase_end(ExecutionPhase.LLM, False)
                return False

            from jobcli.intelligence.memory import AgentMemory
            from jobcli.intelligence.synonym_resolver import SynonymResolver

            memory = AgentMemory(
                self.session,
                infer_location_country=self.config.infer_location_country,
                job_id=state.job_id,
            )
            synonym_resolver = SynonymResolver(infer_location_country=self.config.infer_location_country)

            # Give the AgentInterface DB access so every human prompt checks
            # memory first and every human answer is auto-persisted.
            agent.set_context(
                memory=memory,
                common_questions=self.common_questions,
            )

            task = "fill_form_fields_only" if apply_was_clicked else "find_apply_button_and_fill_form"

            executor = ToolExecutor(
                page, logger, memory=memory,
                synonym_resolver=synonym_resolver,
                ats_type=state.detected_ats,
                ats_handler=rules_handler,
            )

            # ── Pre-LLM gap analysis (hints only — no autonomous fills) ──────
            # Build the enriched gap list (with suggested_value hints from
            # resume + memory) that the LLM auditor uses in its prompt.
            # No browser fill actions are executed here; the LLM is the sole
            # executor. Memory prefill autonomous execution is disabled.
            enriched_gaps, _ = self._prepare_gaps_for_llm(
                ax_tree, page, memory, synonym_resolver, state
            )

            # ── Phase 3/5: LLM agent loop ─────────────────────────────────
            logger.log_phase_start(ExecutionPhase.LLM)
            state.current_phase = ExecutionPhase.LLM
            agent.show_phase_banner("LLM autofill (3/5)")
            logger.info(
                "Phase 3/5: LLM agent (extension, rules, memory prefill complete)",
                phase=ExecutionPhase.LLM,
            )

            MAX_ASK_LOOPS = 3
            loop_count = 0
            results: dict = {}
            performed_uploads: set = set()
            human_submitted_during_fill = False

            # ── Inner fill loop (handles ASK retries) ──────────────────
            while loop_count < MAX_ASK_LOOPS:
                self._check_stop()
                if human_submitted_during_fill:
                    break
                if self._submission_looks_plausible(page):
                    human_submitted_during_fill = True
                    agent.show_success(
                        "Confirmation page detected — skipping further autofill."
                    )
                    logger.info(
                        "confirmation_page_before_llm_iter — stopping fill loop",
                        phase=ExecutionPhase.LLM,
                    )
                    break
                loop_count += 1
                agent.show_status(f"AI iteration {loop_count}/{MAX_ASK_LOOPS}", phase=ExecutionPhase.LLM)

                # Before calling the LLM or touching the page, make sure no
                # bot-verification challenge is currently on screen.  If it
                # is, pause the whole loop until the human clears it — the
                # LLM would only see "verify you are human" text anyway.
                if not self._freeze_if_verification(
                    page, agent, logger, context_label=f"llm_iter_{loop_count}"
                ):
                    logger.log_phase_end(ExecutionPhase.LLM, False)
                    return False
                # Re-extract snapshot every time so the LLM sees the
                # results of previous steps.
                agent.show_status("Extracting page structure (Accessibility Tree)...", phase=ExecutionPhase.LLM)
                try:
                    ax_tree = extractor.extract()
                except Exception:
                    pass

                ax_tree, enriched_gaps = self._run_memory_prefill(
                    page, ax_tree, extractor, executor, memory,
                    synonym_resolver, state, logger, agent,
                )
                
                # ── Fallback: if enriched_gaps is empty, try to extract all form fields as gaps ──
                if not enriched_gaps and ax_tree.form_fields:
                    logger.warning(
                        f"Gap enrichment returned 0 gaps but AX tree has {len(ax_tree.form_fields)} form_fields. "
                        "Falling back to raw form_fields as gaps for LLM.",
                        phase=ExecutionPhase.LLM,
                    )
                    enriched_gaps = build_empty_fields_payload(
                        ax_tree,
                        dropdown_options=ax_tree.dropdown_fields,
                        extra_gap_labels=_unselected_required_dropdowns(page),
                        include_optional=True,
                    )
                    # If still empty, pass aria snapshot text directly to LLM without gap targeting
                    if not enriched_gaps:
                        logger.warning(
                            "No gaps found even with include_optional=True. "
                            "LLM will rely on raw aria snapshot for field discovery.",
                            phase=ExecutionPhase.LLM,
                        )
                
                gap_hints = memory.build_gap_hints(
                    enriched_gaps,
                    self.resume,
                    state.detected_ats,
                    common_questions=self.common_questions,
                )

                # ── Diagnostic: log gap count so users can verify the LLM is receiving fields ──
                if enriched_gaps is not None:
                    req_count = sum(1 for g in enriched_gaps if g.get("required"))
                    opt_count = len(enriched_gaps) - req_count
                    logger.info(
                        f"LLM TARGET GAPS — required: {req_count}, optional: {opt_count}, total: {len(enriched_gaps)}",
                        phase=ExecutionPhase.LLM,
                    )
                    if not enriched_gaps:
                        logger.warning(
                            "TARGET GAPS is EMPTY — LLM has no fields to fill. "
                            "This usually means all form fields are already filled or "
                            "the AX tree did not extract any form fields from the page.",
                            phase=ExecutionPhase.LLM,
                        )
                    else:
                        # Log the actual gaps for debugging
                        gap_labels = [g.get("label", "") for g in enriched_gaps[:10]]
                        logger.debug(
                            f"GAP DETAILS — fields to fill: {gap_labels}" +
                            (f" ... and {len(enriched_gaps) - 10} more" if len(enriched_gaps) > 10 else ""),
                            phase=ExecutionPhase.LLM,
                        )
                        # Log required fields specifically
                        req_labels = [g.get("label", "") for g in enriched_gaps if g.get("required")]
                        if req_labels:
                            logger.debug(
                                f"REQUIRED GAPS: {req_labels}",
                                phase=ExecutionPhase.LLM,
                            )
                else:
                    logger.warning("enriched_gaps is None — LLM will use raw AX tree without gap targeting.", phase=ExecutionPhase.LLM)

                memory_context = memory.combined_llm_memory_block(
                    self.resume,
                    state.detected_ats,
                    common_questions=self.common_questions,
                    gap_hints=gap_hints,
                )
                prefilled_snapshot = self._snapshot_filled(page)
                prefilled_fields = self._build_prefilled_field_lines(
                    prefilled_snapshot, ax_tree
                )
                if prefilled_fields:
                    memory_context += (
                        "\n\n## ALREADY FILLED FIELDS (DO NOT re-fill — extension/rules/prior steps):\n"
                        + "\n".join(prefilled_fields)
                    )
                agent.show_status(f"AI is thinking — Consulting {provider}...", phase=ExecutionPhase.LLM)
                # ``analyze_page_from_axtree`` raises ``TLSConnectionError``
                # immediately on first attempt when the failure is a TLS-trust
                # issue (retrying never helps), so we render a remediation
                # message instead of the generic "AI unavailable" hand-off.
                from jobcli.llm.client import TLSConnectionError
                if not llm_client:
                    logger.error("LLM client not initialized.", phase=ExecutionPhase.LLM)
                    logger.log_phase_end(ExecutionPhase.LLM, False)
                    return False
                try:
                    llm_response = llm_client.analyze_page_from_axtree(
                        ax_tree, self.resume, task=task,
                        memory_context=memory_context,
                        dropdown_options=ax_tree.dropdown_fields,
                        resume_pdf_path=resume_pdf_path,
                        extra_gap_labels=_unselected_required_dropdowns(page),
                        prefilled_field_lines=prefilled_fields or None,
                        enriched_gaps=enriched_gaps or None,
                        gap_hints=gap_hints or None,
                    )
                except TLSConnectionError as tls_err:
                    agent.show_error(
                        "AI provider unreachable — TLS certificate verification failed."
                    )
                    handoff = agent.handoff_to_human(
                        reason=f"TLS handshake to the AI provider failed. {tls_err}",
                        hint=(
                            "This is NOT a quota or API-key issue — your Python "
                            "ssl module can't validate the AI provider's HTTPS "
                            "cert. Fix it once and 'jobcli apply' will Just Work:\n"
                            "  1. Preferred — install your corporate root CA into "
                            "Windows 'Trusted Root Certification Authorities' (or "
                            "macOS Keychain) and restart your terminal.\n"
                            "  2. Or set JOBCLI_SSL_CA_BUNDLE=<path-to-ca.pem> "
                            "pointing at a PEM containing the chain root.\n"
                            "  3. Last resort — set JOBCLI_INSECURE_TLS=1 in this "
                            "shell to skip verification (insecure).\n"
                            "You can still complete THIS form manually now."
                        ),
                    )
                    logger.log_phase_end(ExecutionPhase.LLM, not handoff.cancelled)
                    if self._handoff_skipped_job(handoff, agent, logger, ExecutionPhase.LLM):
                        return False
                    if handoff.cancelled:
                        return False
                    page = handoff.page
                    agent.page = page
                    return handoff.advanced or self._submission_looks_plausible(page)

                if not llm_response:
                    # Most common cause: 429 insufficient_quota, transient
                    # network failure, or provider outage.  Don't drop the
                    # user — hand them the browser with a clear visual cue
                    # so they can finish the form themselves.
                    agent.show_error(
                        "AI is unavailable (no response) — handing the form to you."
                    )
                    handoff = agent.handoff_to_human(
                        reason="The AI provider returned no response (likely API quota exhausted or network error).",
                        hint="Finish the form yourself in the browser. When done, "
                             "press ENTER and JobCLI will resume from your current page.",
                    )
                    logger.log_phase_end(ExecutionPhase.LLM, not handoff.cancelled)
                    if self._handoff_skipped_job(handoff, agent, logger, ExecutionPhase.LLM):
                        return False
                    if handoff.cancelled:
                        return False
                    page = handoff.page
                    agent.page = page
                    # Trust the human: if they advanced the page, treat as success.
                    return handoff.advanced or self._submission_looks_plausible(page)

                if llm_response.requires_human:
                    logger.warning("LLM flagged requires_human — proceeding with actions anyway")

                # ── Don't-refill filter ───────────────────────────────────
                # Refresh the live snapshot for this iteration and drop any
                # FILL / TYPE / SELECT whose target already has a value.
                # The LLM regularly re-proposes fills for fields the
                # extension or a prior phase already populated; without
                # this guard those re-fills overwrite good values and the
                # user sees the same field flicker filled twice or more.
                prefilled_snapshot = self._snapshot_filled(page)
                if prefilled_snapshot and llm_response.actions:
                    kept: list[BrowserAction] = []
                    for act in llm_response.actions:
                        hit = self._action_target_already_filled(act, prefilled_snapshot)
                        if hit is None:
                            kept.append(act)
                            continue
                        logger.info(
                            f"Skipping re-fill of '{act.field_label or act.selector}' "
                            f"(already has '{hit}')",
                            phase=ExecutionPhase.LLM,
                        )
                    llm_response.actions = kept

                # ── Handle ASK actions: STOP-AND-WAIT semantics ──────────
                # When the AI requests info, we do NOT execute any other
                # actions in this iteration.  We pause, gather every missing
                # answer (DB-first via the agent), persist new answers to
                # memory, then re-run the LLM so it sees the enriched memory
                # context and proposes proper FILL actions next time.
                ask_actions = [
                    a for a in llm_response.actions if a.action == ActionType.ASK
                ]
                optional_asks = [
                    a for a in ask_actions if not a.required
                ]
                ask_actions = [a for a in ask_actions if a.required]
                if optional_asks:
                    logger.info(
                        f"Ignoring {len(optional_asks)} optional ASK action(s) "
                        "(only required * fields are prompted in the terminal).",
                        phase=ExecutionPhase.LLM,
                    )
                if ask_actions:
                    agent.show_status(
                        f"AI requested {len(ask_actions)} required answer(s) — pausing all other actions.",
                        phase=ExecutionPhase.HUMAN,
                    )
                    resolved_actions: list[BrowserAction] = []
                    for act in ask_actions:
                        label = act.field_label or act.selector
                        options = None
                        is_dropdown = False
                        for dp in ax_tree.dropdown_fields:
                            if dp["label"].lower() == label.lower():
                                options = dp["options"]
                                is_dropdown = True
                                break
                        # request_field_input does DB-lookup-first and persists
                        # any new human answer automatically.
                        answer = agent.request_field_input(
                            label,
                            options=options,
                            question_text=act.value,
                            required=True,
                        )
                        if not answer:
                            continue
                        from jobcli.utils.fill_guard import is_reserved_form_value

                        if is_reserved_form_value(answer):
                            continue
                        # Turn the ASK into a concrete browser action.  Using
                        # ``label`` as the selector lets ``_execute_type`` and
                        # ``_execute_select`` match by <label for>, aria-label,
                        # placeholder, name/id attribute, etc. — covering the
                        # ATS form patterns we care about.
                        act.selector = label
                        act.value = answer
                        act.action = ActionType.SELECT if is_dropdown else ActionType.FILL
                        resolved_actions.append(act)

                    if resolved_actions:
                        # Execute the resolved fills IMMEDIATELY.  The old
                        # behaviour of just mutating + ``continue`` silently
                        # dropped the values because the actions never reached
                        # the executor — the model rarely re-proposes the
                        # same fill on the next iteration, so the answer
                        # would sit in memory but never land in the DOM.
                        agent.show_status(
                            f"Applying {len(resolved_actions)} resolved answer(s) "
                            "to the browser…",
                            phase=ExecutionPhase.LLM,
                        )
                        resolved_response = LLMActionResponse(
                            reasoning="Executing ASK-resolved answers.",
                            actions=resolved_actions,
                            requires_human=False,
                            page_complete=False,
                        )
                        # Dropdown safety net: ensure FILL→SELECT coercion
                        # runs on these too (e.g. "Commutable distance" that
                        # the LLM tagged as a text field but is really a
                        # combobox).
                        _coerce_dropdown_actions(resolved_response, ax_tree, logger)

                        ask_results = executor.execute_actions(resolved_response)
                        ask_succeeded = sum(
                            1 for v in ask_results.values() if v
                        )
                        agent.show_status(
                            f"Applied {ask_succeeded}/{len(resolved_actions)} "
                            "resolved answer(s).",
                            phase=ExecutionPhase.LLM,
                        )
                        # Persist successful locators so future runs hit them
                        # directly without needing the LLM again.
                        page_domain = _safe_domain(page.url)
                        for i, act in enumerate(resolved_actions):
                            if ask_results.get(f"action_{i}_{act.action.value}"):
                                try:
                                    self.locator_repo.upsert_for_field(
                                        ats_type=state.detected_ats,
                                        domain=page_domain,
                                        purpose=(act.field_label or act.selector or "")[:100],
                                        selector=act.selector,
                                        selector_type=act.selector_type,
                                        success=True,
                                        job_id=state.job_id,
                                    )
                                except Exception:
                                    pass

                        # Re-run the LLM cycle so it sees the filled fields
                        # and can tackle whatever is left.
                        agent.show_status(
                            "Memory updated — re-running AI with new context.",
                            phase=ExecutionPhase.LLM,
                        )
                        ax_tree = extractor.extract()
                        continue
                    # No answers gathered — fall through and execute whatever
                    # non-ASK actions the model proposed.

                # ── Upload prioritisation ─────────────────────────────────
                has_upload = any(a.action == ActionType.UPLOAD for a in llm_response.actions)
                if has_upload:
                    new_uploads = []
                    for act in llm_response.actions:
                        if act.action == ActionType.UPLOAD:
                            upload_key = str(act.value).split('/')[-1].split('\\')[-1]
                            if upload_key not in performed_uploads:
                                new_uploads.append(act)
                                performed_uploads.add(upload_key)
                    if new_uploads:
                        llm_response.actions = new_uploads
                        agent.show_status("Upload detected — prioritising and re-scanning for autofill.", phase=ExecutionPhase.LLM)
                    else:
                        has_upload = False
                        llm_response.actions = [a for a in llm_response.actions if a.action != ActionType.UPLOAD]

                # ── Show action plan / get approval ───────────────────────
                _strip_apply_clicks_when_filling_only(llm_response, task)
                _strip_third_party_apply_clicks(llm_response, logger)
                # Generic dropdown safety net: any FILL/TYPE that targets a
                # known dropdown label is rewritten to SELECT so the executor
                # opens the dropdown instead of typing into the closed widget.
                _coerce_dropdown_actions(llm_response, ax_tree, logger)
                llm_response.actions = [a for a in llm_response.actions if a.action != ActionType.ASK]

                # ── Required-fields-first gate ────────────────────────────
                # If the LLM wants to click Next/Continue/Submit/Apply but
                # required (*) fields are still empty, hold those clicks back,
                # surface the missing labels, and ask the human to fill them
                # via the modal (which itself checks DB-memory first).
                non_advance, advance_clicks = _split_off_advance_clicks(llm_response)
                empty_required = _empty_required_fields(ax_tree)
                if advance_clicks and empty_required:
                    agent.show_warning(
                        f"Holding {len(advance_clicks)} Next/Submit click(s) — "
                        f"{len(empty_required)} required field(s) still empty: "
                        + ", ".join(empty_required[:6])
                        + ("…" if len(empty_required) > 6 else "")
                    )
                    for label in empty_required:
                        options = None
                        for dp in (ax_tree.dropdown_fields or []):
                            if _normalize_label(dp.get("label", "")) == _normalize_label(label):
                                options = dp.get("options") or None
                                break
                        agent.request_field_input(
                            label,
                            options=options,
                            question_text=f"Required field '{label}' is empty.",
                            required=True,
                        )
                    # Drop the advance clicks for this iteration; only execute
                    # the non-advance actions (fills/selects/uploads).  The
                    # next loop iteration will re-extract the AX tree and the
                    # LLM will re-plan, this time with the human's new answers.
                    llm_response.actions = non_advance

                if not agent.approve_action_plan(llm_response.actions):
                    agent.show_warning("Action plan rejected — skipping this iteration.")
                    break

                # ── Execute actions ───────────────────────────────────────
                ctx = page.context
                pids0 = {id(p) for p in ctx.pages}
                url0, n0 = page.url, len(ctx.pages)
                results = executor.execute_actions(llm_response)

                # ── Post-execution validation: check if required fields are still empty ──
                try:
                    post_ax = extractor.extract()
                    from jobcli.llm.empty_fields import build_empty_fields_payload as _bef
                    still_empty_required = [
                        g["label"] for g in _bef(post_ax, include_optional=False)
                    ]
                    if still_empty_required:
                        logger.warning(
                            f"After LLM pass: {len(still_empty_required)} required field(s) still empty: "
                            + ", ".join(still_empty_required[:8]),
                            phase=ExecutionPhase.LLM,
                        )
                    else:
                        logger.info(
                            "Post-LLM validation: all required fields appear filled.",
                            phase=ExecutionPhase.LLM,
                        )
                except Exception as _ve:
                    logger.debug(f"Post-LLM validation scan failed: {_ve}", phase=ExecutionPhase.LLM)

                # ── Tally successful LLM fills for field-ownership report ─
                _fill_verbs = ("fill", "type", "select", "click")
                _llm_ok = sum(
                    1 for k, v in results.items()
                    if v and any(vb in k for vb in _fill_verbs)
                )
                self._last_llm_filled_count = getattr(self, "_last_llm_filled_count", 0) + _llm_ok

                adopted = adopt_application_page_after_action(
                    page, page_count_before=n0, url_before=url0,
                    page_ids_before=pids0, logger=logger,
                )
                if id(adopted) != id(page):
                    page = adopted
                    agent.page = page
                    # New tab means the handler's ``page`` reference is
                    # now stale — rebind it so ``click_option`` talks
                    # to the right page.
                    if rules_handler is not None:
                        try:
                            rules_handler.page = page
                        except Exception:
                            pass
                    executor = ToolExecutor(
                        page, logger, memory=memory,
                        synonym_resolver=synonym_resolver,
                        ats_type=state.detected_ats,
                        ats_handler=rules_handler,
                    )
                    extractor = AccessibilityTreeExtractor(page)
                    agent.show_status("Followed new tab.", phase=ExecutionPhase.LLM)
                    self._dismiss_cookie_consent(page, logger)
                else:
                    page = adopted

                if has_upload:
                    # Give the ATS backend enough time to parse the resume
                    # and autofill the fields *before* we scan the DOM again.
                    # Usually takes ~4-7 seconds for the React state to update.
                    wait_time = 8000
                    agent.show_status(
                        f"Upload done — waiting {wait_time/1000}s, then extension (rules only if needed)…",
                        phase=ExecutionPhase.LLM,
                    )
                    page.wait_for_timeout(wait_time)
                    prefilled_snapshot = self._run_extension_autofill_phase(
                        page, logger, agent
                    )
                    # Rules post-upload pass runs ONLY if the post-upload
                    # extension run produced zero new fills. Matches the
                    # pre-upload fallback semantics (see Part B.1 above).
                    if rules_handler is not None and self._last_extension_filled_count == 0:
                        try:
                            agent.show_status(
                                "Extension post-upload filled nothing — running rules as fallback…",
                                phase=ExecutionPhase.RULES,
                            )
                            post_upload_results = self._run_rules_prefill(
                                rules_handler, page, logger, agent, extractor, state
                            )
                            self._last_rules_filled_count += sum(
                                1 for v in (post_upload_results or {}).values() if v
                            )
                            prefilled_snapshot = self._snapshot_filled(page)
                            page.wait_for_timeout(500)
                        except Exception as e:
                            logger.debug(
                                f"Post-upload rules pass raised {e!r}",
                                phase=ExecutionPhase.RULES,
                            )
                    elif rules_handler is not None:
                        logger.info(
                            f"Post-upload extension filled {self._last_extension_filled_count} "
                            "field(s) — skipping rules fallback.",
                            phase=ExecutionPhase.RULES,
                        )
                    ax_tree = extractor.extract()
                    continue

                ax_tree = extractor.extract()

                # Save successful actions to memory + persist locators per
                # ATS+domain so future runs (same site or sibling employer on
                # the same ATS) can short-circuit element discovery.
                page_domain = _safe_domain(page.url)
                for action in llm_response.actions:
                    action_success = results.get(f"action_{llm_response.actions.index(action)}_{action.action.value}", False)
                    if action_success and action.value and action.action in (ActionType.FILL, ActionType.TYPE, ActionType.SELECT):
                        label = action.field_label or action.selector
                        memory.save_field_answer(label, action.value, state.detected_ats, success=True, source="llm")
                        if executor.last_successful_strategy:
                            memory.save_interaction(state.detected_ats, action.action.value, label, action.selector, executor.last_successful_strategy, True, page.url)
                        # Domain-aware learned locator (idempotent upsert).
                        try:
                            self.locator_repo.upsert_for_field(
                                ats_type=state.detected_ats,
                                domain=page_domain,
                                purpose=f"{action.action.value}:{_normalize_label(label)}",
                                selector=action.selector,
                                selector_type=action.selector_type,
                                success=True,
                                job_id=state.job_id,
                            )
                        except Exception as e:
                            logger.debug(f"locator persist skipped: {e}", phase=ExecutionPhase.LLM)
                        state.step_count += 1
                        # Record successful execution back into memory so
                        # confidence scores reflect real browser outcomes.
                        if action.field_label and action.value:
                            try:
                                memory.record_field_outcome(
                                    field_label=action.field_label,
                                    value=action.value,
                                    success=True,
                                    ats_type=state.detected_ats,
                                )
                            except Exception:
                                pass
                    elif action.action in (ActionType.FILL, ActionType.TYPE, ActionType.SELECT) and not action_success:
                        # Track failures too so confidence scoring stays honest.
                        try:
                            self.locator_repo.upsert_for_field(
                                ats_type=state.detected_ats,
                                domain=page_domain,
                                purpose=f"{action.action.value}:{_normalize_label(action.field_label or action.selector)}",
                                selector=action.selector,
                                selector_type=action.selector_type,
                                success=False,
                                job_id=state.job_id,
                            )
                        except Exception:
                            pass
                        # Record failed execution so confidence degrades correctly.
                        if action.field_label and action.value:
                            try:
                                memory.record_field_outcome(
                                    field_label=action.field_label,
                                    value=action.value,
                                    success=False,
                                    ats_type=state.detected_ats,
                                )
                            except Exception:
                                pass

                # ── Handle failed fields ──────────────────────────────────
                # There are two distinct buckets of failure:
                #   (a) VALUE MISSING — the LLM proposed a fill/select but
                #       provided no value.  ``show_failed_fields`` asks the
                #       human for each (DB-first), returns BrowserActions
                #       with values filled in, and we re-execute them.
                #   (b) SELECTOR BAD — the LLM proposed a fill with a value
                #       but the selector didn't match the DOM (common on
                #       Ashby / custom ATS).  Asking the user for the same
                #       value again won't help — we need a human pair of
                #       eyes on the browser to finish the form.
                failed_actions = executor.get_failed_actions()
                if failed_actions:
                    # Bucket (a): fields missing a value.
                    missing_value = [
                        a for a in failed_actions
                        if a.action in (ActionType.FILL, ActionType.TYPE, ActionType.SELECT)
                        and not (a.value and str(a.value).strip())
                    ]
                    # Bucket (b): fields that had a value but the selector
                    # refused it.
                    selector_failed = [
                        a for a in failed_actions
                        if a.action in (ActionType.FILL, ActionType.TYPE, ActionType.SELECT)
                        and (a.value and str(a.value).strip())
                    ]

                    retry_failed: list[BrowserAction] = []
                    if missing_value:
                        filled_actions = agent.show_failed_fields(
                            missing_value,
                            dropdown_options_by_selector=getattr(executor, "last_dropdown_options", None),
                        )
                        if filled_actions:
                            agent.show_status(
                                f"Re-executing {len(filled_actions)} field(s) with your answers…",
                                phase=ExecutionPhase.LLM,
                            )
                            retry_succeeded = 0
                            page_domain = _safe_domain(page.url)
                            for act in filled_actions:
                                ok = False
                                try:
                                    ok = executor.execute_action(act)
                                except Exception as e:
                                    logger.error(
                                        f"Retry for '{act.field_label}' raised: {e}",
                                        phase=ExecutionPhase.LLM,
                                    )
                                if ok:
                                    retry_succeeded += 1
                                    label = act.field_label or act.selector
                                    if act.value:
                                        memory.save_field_answer(
                                            label, act.value, state.detected_ats,
                                            success=True, source="human",
                                        )
                                    try:
                                        self.locator_repo.upsert_for_field(
                                            ats_type=state.detected_ats,
                                            domain=page_domain,
                                            purpose=f"{act.action.value}:{_normalize_label(label)}",
                                            selector=act.selector,
                                            selector_type=act.selector_type,
                                            success=True,
                                            job_id=state.job_id,
                                        )
                                    except Exception:
                                        pass
                                else:
                                    retry_failed.append(act)
                            agent.show_status(
                                f"Retry result: {retry_succeeded}/{len(filled_actions)} filled successfully.",
                                phase=ExecutionPhase.LLM,
                            )

                    # ── Collect anything that's still unfinished ─────────
                    for act in (retry_failed + selector_failed):
                        logger.warning(
                            f"Action failed on '{act.field_label or act.selector}' ({act.action.value})",
                            phase=ExecutionPhase.LLM,
                            value=act.value,
                        )

                    still_broken = selector_failed + retry_failed

                    # If clicking buttons failed and that was the only thing that failed, we must not
                    # swallow it! Catch the un-filtered failures so we don't infinitely skip them.
                    if not still_broken and failed_actions:
                        still_broken = failed_actions

                    if still_broken:
                        resolved_count = 0
                        if self.config.interaction_mode != InteractionMode.AUTO:
                            for act in list(still_broken):
                                label = act.field_label or act.selector
                                # Try to find options for dropdowns
                                field_options = None
                                if act.action == ActionType.SELECT:
                                    for dp in ax_tree.dropdown_fields:
                                        if dp["label"].lower() == label.lower():
                                            field_options = dp["options"]
                                            break
                                
                                if not act.required:
                                    continue
                                help_val = agent.request_field_input(
                                    label,
                                    current_value=act.value or "",
                                    options=field_options,
                                    required=True,
                                )
                                if help_val:
                                    from jobcli.utils.fill_guard import is_reserved_form_value

                                    if is_reserved_form_value(help_val):
                                        continue
                                    # Human provided a value (or picked an option). 
                                    # Update action and try one more time.
                                    act.value = help_val
                                    from jobcli.llm.client import LLMActionResponse
                                    temp_response = LLMActionResponse(actions=[act])
                                    if executor.execute_actions(temp_response):
                                        still_broken.remove(act)
                                        resolved_count += 1
                                        agent.show_success(f"Successfully filled '{label}' via terminal help.")
                        
                        if still_broken:
                            # Build a "label → value" preview for remaining fields
                            rows = []
                            for a in still_broken[:8]:
                                lbl = a.field_label or a.selector
                                val = (a.value or "").strip()
                                if val:
                                    preview = val if len(val) <= 60 else val[:57] + "…"
                                    rows.append(f"  • {lbl}: {preview}")
                                else:
                                    rows.append(f"  • {lbl}  (no value)")
                            
                            more = f"\n  … and {len(still_broken) - 8} more" if len(still_broken) > 8 else ""
                            pretty_list = "\n".join(rows) + more
                            agent.show_warning(
                                f"{len(still_broken)} field(s) still need attention. "
                                "Please fill them in the browser:\n" + pretty_list
                            )
                            try:
                                _handoff_pre_url = page.url or ""
                            except Exception:
                                _handoff_pre_url = ""
                            try:
                                _handoff_had_submit = bool(_submit_button_visible(page))
                            except Exception:
                                _handoff_had_submit = True
                            handoff = agent.handoff_to_human(
                                reason=(
                                    f"{len(still_broken)} field(s) could not be automated. "
                                    "Please fill them in the browser."
                                ),
                                hint=(
                                    "When done, press ENTER and JobCLI will continue. "
                                    "If you already submitted, JobCLI detects the "
                                    "confirmation page automatically."
                                ),
                                submission_checker=lambda: self._looks_like_confirmation(
                                    agent.page, _handoff_pre_url, _handoff_had_submit
                                ),
                            )
                        else:
                            handoff = None
                        if handoff is not None:
                            if self._handoff_skipped_job(handoff, agent, logger, ExecutionPhase.LLM):
                                logger.log_phase_end(ExecutionPhase.LLM, False)
                                return False
                            if handoff.cancelled:
                                logger.log_phase_end(ExecutionPhase.LLM, False)
                                return False
                            page = handoff.page
                            agent.page = page
                            if self._handoff_indicates_submitted(page, handoff):
                                human_submitted_during_fill = True
                                agent.show_success(
                                    "Application submitted during handoff — "
                                    "skipping further autofill."
                                )
                                logger.info(
                                    "human_submitted_during_failed_field_handoff",
                                    phase=ExecutionPhase.LLM,
                                    url_after=handoff.url_after,
                                    title_after=handoff.title_after,
                                )
                                break
                            # Human may have advanced the form — re-extract so the
                            # next iteration sees the updated page.
                            try:
                                ax_tree = extractor.extract()
                            except Exception:
                                pass

                if human_submitted_during_fill:
                    break

                if not failed_actions and not ask_actions:
                    agent.show_success("All actions completed.")
                    # One-line tally so the user can see who did the work.
                    try:
                        llm_actions = len([
                            a for a in (llm_response.actions or [])
                            if a.action in (ActionType.FILL, ActionType.SELECT, ActionType.CLICK, ActionType.TYPE, ActionType.UPLOAD)
                        ])
                        ext_n = getattr(self, "_last_extension_filled_count", 0) or 0
                        rules_n = getattr(self, "_last_rules_filled_count", 0) or 0
                        logger.info(
                            f"Fill summary — extension: {ext_n} field(s), "
                            f"rules: {rules_n} field(s), "
                            f"LLM iteration {loop_count}: {llm_actions} action(s).",
                            phase=ExecutionPhase.LLM,
                        )
                    except Exception:
                        pass
                    break

            if human_submitted_during_fill:
                logger.log_phase_end(ExecutionPhase.LLM, True)
                return True

            # ── Multi-page form loop ──────────────────────────────────────
            # NOTE: This loop is for Workday-style "Next → Next → Submit"
            # multi-step forms. Single-page ATS (Ashby, Lever, Greenhouse
            # inline forms) should exit after the very first iteration
            # once required fields are satisfied — see early-exit checks
            # below.
            MAX_PAGES = 5
            page_count = 1
            # Track fingerprint of empty-required fields so we can detect
            # "we're going in circles on the same page" and bail out.
            prev_empty_fingerprint: Optional[str] = None

            # Early exit: if required fields are already satisfied AND
            # no URL change is expected, the form is complete on this
            # single page.  Don't re-run the LLM.
            #
            # We combine three signals here because no single one is
            # sufficient on React-rendered ATS forms:
            #   • AXTree  → ``_empty_required_fields(ax_tree)``
            #   • Live DOM custom-dropdown scan → ``_unselected_required_dropdowns``
            #   • Client-side validation errors → ``_live_validation_errors``
            try:
                initial_empty = _empty_required_fields(ax_tree)
            except Exception:
                initial_empty = []
            try:
                initial_dropdown_empty = _unselected_required_dropdowns(page)
            except Exception:
                initial_dropdown_empty = []
            try:
                initial_errors = _live_validation_errors(page)
            except Exception:
                initial_errors = []
            # Workday (and similar wizards like Oracle Cloud) can have every visible field filled on
            # *step 1* while **Continue** / **Next** is still required to reach
            # Experience, Disclosures, Review, etc.  ``_empty_required_fields`` may
            # be empty, so we must not skip the multi-page loop for wizards.
            # We determine if it's a wizard by checking for common Next/Continue buttons in the DOM.
            has_wizard_buttons = False
            try:
                has_wizard_buttons = bool(
                    page.query_selector(
                        "button:has-text('Next'), button:has-text('Continue'), "
                        "button:has-text('Save and Continue'), a:has-text('Next'), "
                        "a:has-text('Continue'), [data-automation-id='bottom-navigation-next-button']"
                    )
                )
            except Exception:
                pass

            is_wizard = state.detected_ats == ATSType.WORKDAY or has_wizard_buttons
            if (
                not is_wizard
                and not initial_empty
                and not initial_dropdown_empty
                and not initial_errors
            ):
                logger.info(
                    "All required fields satisfied on first pass and no Next button found — skipping multi-page loop.",
                    phase=ExecutionPhase.LLM,
                )
                page_count = MAX_PAGES  # sentinel to bypass loop
            elif is_wizard and not initial_empty and not initial_dropdown_empty and not initial_errors:
                logger.info(
                    "Required fields look satisfied, but a Next/Continue button is present — "
                    "running the wizard pass.",
                    phase=ExecutionPhase.LLM,
                )
            elif initial_dropdown_empty or initial_errors:
                logger.info(
                    "Form has unresolved required dropdowns or validation errors — "
                    f"empty_required={len(initial_empty)} "
                    f"empty_dropdowns={len(initial_dropdown_empty)} "
                    f"validation_errors={len(initial_errors)}. "
                    "Multi-page loop will run a repair pass.",
                    phase=ExecutionPhase.LLM,
                )

            while page_count < MAX_PAGES:
                total = len(results)
                successes = sum(1 for v in results.values() if v)

                # Low success rate is a strong signal that the LLM's
                # selectors didn't match the DOM — do NOT silently proceed
                # to submit, hand the form off to the human so they can
                # finish it in the browser.  This is what lets the user
                # recover from an Ashby / custom-ATS page where the fills
                # failed even though the LLM supplied values.
                if total == 0 or (successes / total) < 0.5:
                    logger.info(
                        f"Page {page_count}: {successes}/{total} actions succeeded — "
                        "handing off to human.",
                        phase=ExecutionPhase.LLM,
                    )
                    # Also check for required fields that are still empty so
                    # the handoff message is specific.
                    try:
                        required_but_empty = _empty_required_fields(ax_tree)
                    except Exception:
                        required_but_empty = []
                    hint_extra = ""
                    if required_but_empty:
                        preview = ", ".join(required_but_empty[:6])
                        if len(required_but_empty) > 6:
                            preview += " …"
                        hint_extra = f" Required fields still empty: {preview}."
                    handoff = agent.handoff_to_human(
                        reason=(
                            f"Only {successes}/{total} auto-fills succeeded on this "
                            "page. JobCLI is handing control to you to finish it."
                        ),
                        hint=(
                            "Complete every required field in the browser. "
                            "When you're done (or after clicking Next/Submit), "
                            "press ENTER and JobCLI will continue."
                            + hint_extra
                        ),
                    )
                    if self._handoff_skipped_job(handoff, agent, logger, ExecutionPhase.LLM):
                        logger.log_phase_end(ExecutionPhase.LLM, False)
                        return False
                    if handoff.cancelled:
                        logger.log_phase_end(ExecutionPhase.LLM, False)
                        return False
                    page = handoff.page
                    agent.page = page
                    break

                agent.show_status(f"Page {page_count}: {successes}/{total} actions succeeded.", phase=ExecutionPhase.LLM)

                # Check for still-empty mandatory fields
                required_but_empty = []
                for field in ax_tree.form_fields:
                    is_required = field.get("required") or "*" in field.get("label", "")
                    if is_required and not field.get("value"):
                        required_but_empty.append(field.get("label") or field.get("name"))
                if required_but_empty:
                    for lbl in required_but_empty:
                        agent.show_warning(f"Mandatory field '{lbl}' still empty.")

                # Checkpoint: let human review / manually fix in the browser
                agent.pause_for_review(
                    f"Page {page_count} filled. Review the browser and fix any empty fields.",
                    timeout_seconds=8,
                )

                page.wait_for_timeout(3000)
                self._dismiss_cookie_consent(page, logger)

                # CAPTCHA check — handled through agent
                anti_bot = AntiBotManager(logger)
                if anti_bot.detect_captcha(page):
                    if not agent.handle_captcha():
                        logger.log_phase_end(ExecutionPhase.LLM, False)
                        return False

                page.wait_for_timeout(2000)
                new_ax_tree = extractor.extract()

                # Learn manually-filled fields from browser
                filled_fields = []
                placeholders = ["select", "choose", "please choose", "select...", "select an option"]
                for field in new_ax_tree.form_fields:
                    val = str(field.get("value", "")).strip()
                    label = field.get("name", "unknown")
                    if val.lower() not in placeholders and val:
                        filled_fields.append(f"- {label}: already has value '{val}'")
                        if memory.save_field_answer(label, val, state.detected_ats, source="human"):
                            logger.info(f"Learned answer for '{label}' from browser.", phase=ExecutionPhase.LLM)

                url_changed = new_ax_tree.url != ax_tree.url
                fields_changed = False
                if len(new_ax_tree.form_fields) != len(ax_tree.form_fields):
                    fields_changed = True
                else:
                    for i, field in enumerate(new_ax_tree.form_fields):
                        old_field = ax_tree.form_fields[i]
                        if str(field.get("value", "")).strip() != str(old_field.get("value", "")).strip() or \
                           bool(field.get("checked")) != bool(old_field.get("checked")):
                            fields_changed = True
                            break

                button_clicked = any(a.action == ActionType.CLICK for a in (llm_response.actions if 'llm_response' in locals() else []))

                # ── Early-exit: single-page form is complete ────────────
                # If URL didn't change AND no required fields remain empty,
                # the form is done and we should NOT keep re-asking the
                # LLM. This is what stops Ashby/Lever/etc. from being
                # treated as multi-page.
                try:
                    new_empty = _empty_required_fields(new_ax_tree)
                except Exception:
                    new_empty = []
                if not url_changed and not new_empty and not is_wizard:
                    agent.show_status(
                        "Form complete on this page — no required fields left.",
                        phase=ExecutionPhase.LLM,
                    )
                    break

                # ── Circle-breaker ─────────────────────────────────────
                # If we're on the same URL and the set of empty-required
                # fields is identical to the previous pass, re-running
                # the LLM won't help — the same selectors will fail the
                # same way. Hand off to the human instead of looping.
                current_fingerprint = (
                    new_ax_tree.url + "|" + "|".join(sorted(new_empty))
                )
                if (
                    not url_changed
                    and prev_empty_fingerprint is not None
                    and current_fingerprint == prev_empty_fingerprint
                ):
                    preview = ", ".join(new_empty[:6]) + (" …" if len(new_empty) > 6 else "")
                    logger.info(
                        "Same page, same empty fields as last pass — stopping "
                        "the re-scan loop.",
                        phase=ExecutionPhase.LLM,
                    )
                    handoff = agent.handoff_to_human(
                        reason=(
                            "The agent has re-scanned this page and can't make "
                            f"further progress. Still missing: {preview}"
                            if preview else
                            "The agent has re-scanned this page and can't make "
                            "further progress automatically."
                        ),
                        hint="Finish the remaining fields in the browser and "
                             "press ENTER to continue.",
                    )
                    if self._handoff_skipped_job(handoff, agent, logger, ExecutionPhase.LLM):
                        logger.log_phase_end(ExecutionPhase.LLM, False)
                        return False
                    if handoff.cancelled:
                        logger.log_phase_end(ExecutionPhase.LLM, False)
                        return False
                    page = handoff.page
                    agent.page = page
                    break
                prev_empty_fingerprint = current_fingerprint

                if not url_changed and not fields_changed and not button_clicked:
                    break

                page_count += 1
                if url_changed:
                    agent.show_status("Navigated to new page.", phase=ExecutionPhase.LLM)
                    self._dismiss_cookie_consent(page, logger)
                else:
                    # Not a new page — we're just re-scanning to see if
                    # earlier actions revealed new fields. Use language
                    # that reflects that so the log isn't misleading.
                    agent.show_status(
                        f"Re-scanning same page (pass {page_count}) after actions.",
                        phase=ExecutionPhase.LLM,
                    )

                ax_tree = new_ax_tree

                mandatory_keywords = ["gender", "veteran", "disability", "authorization", "visa", "legal"]
                found_in_tree = any(any(k in f.get("name", "").lower() for k in mandatory_keywords) for f in ax_tree.form_fields)
                if not found_in_tree and page_count < 4:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(2000)
                    ax_tree = extractor.extract()

                agent.show_status(f"Running AI on page {page_count}...", phase=ExecutionPhase.LLM)

                # ── TalentScreen Extension Autofill (per page) ────────────
                # TalentScreen handles each page automatically via its own
                # content scripts. No click trigger needed from the CLI.
                # ──────────────────────────────────────────────────────────

                logger.save_structured_dom(ax_tree.model_dump(), f"ax_tree_page_{page_count}", ExecutionPhase.LLM)

                # Rebuild gap hints from current page state (no autonomous fills).
                enriched_gaps, gap_hints = self._prepare_gaps_for_llm(
                    ax_tree, page, memory, synonym_resolver, state
                )

                filled_context = ""
                if filled_fields:
                    filled_context = "\n\n## ALREADY FILLED FIELDS (DO NOT re-fill these — they were set by the autofill extension):\n" + "\n".join(filled_fields)
                memory_context = memory.combined_llm_memory_block(
                    self.resume,
                    state.detected_ats,
                    common_questions=self.common_questions,
                    gap_hints=gap_hints,
                ) + filled_context

                if not llm_client:
                    logger.error("LLM client not initialized.", phase=ExecutionPhase.LLM)
                    logger.log_phase_end(ExecutionPhase.LLM, False)
                    return False
                llm_response = llm_client.analyze_page_from_axtree(
                    ax_tree, self.resume, task="fill_empty_fields_only",
                    memory_context=memory_context,
                    dropdown_options=ax_tree.dropdown_fields,
                    resume_pdf_path=resume_pdf_path,
                    extra_gap_labels=_unselected_required_dropdowns(page),
                    prefilled_field_lines=filled_fields or None,
                    enriched_gaps=enriched_gaps or None,
                    gap_hints=gap_hints or None,
                )
                if not llm_response:
                    break

                _strip_apply_clicks_when_filling_only(llm_response, "fill_empty_fields_only")
                _strip_third_party_apply_clicks(llm_response, logger)
                _coerce_dropdown_actions(llm_response, ax_tree, logger)
                llm_response.actions = [a for a in llm_response.actions if a.action != ActionType.ASK]

                # Required-fields-first gate (same as the inner loop).
                non_advance2, advance2 = _split_off_advance_clicks(llm_response)
                empty_req2 = _empty_required_fields(ax_tree)
                if advance2 and empty_req2:
                    agent.show_warning(
                        f"Holding {len(advance2)} Next/Submit click(s) on page {page_count} — "
                        f"{len(empty_req2)} required field(s) still empty."
                    )
                    for label in empty_req2:
                        options = None
                        for dp in (ax_tree.dropdown_fields or []):
                            if _normalize_label(dp.get("label", "")) == _normalize_label(label):
                                options = dp.get("options") or None
                                break
                        agent.request_field_input(
                            label,
                            options=options,
                            question_text=f"Required field '{label}' is empty.",
                            required=True,
                        )
                    llm_response.actions = non_advance2

                if not agent.approve_action_plan(llm_response.actions):
                    break

                ctx2 = page.context
                pids1 = {id(p) for p in ctx2.pages}
                url1, n1 = page.url, len(ctx2.pages)
                results = executor.execute_actions(llm_response)

                adopted2 = adopt_application_page_after_action(
                    page, page_count_before=n1, url_before=url1,
                    page_ids_before=pids1, logger=logger,
                )
                if id(adopted2) != id(page):
                    page = adopted2
                    agent.page = page
                    if rules_handler is not None:
                        try:
                            rules_handler.page = page
                        except Exception:
                            pass
                    executor = ToolExecutor(
                        page, logger, memory=memory,
                        synonym_resolver=synonym_resolver,
                        ats_type=state.detected_ats,
                        ats_handler=rules_handler,
                    )
                    extractor = AccessibilityTreeExtractor(page)
                    self._dismiss_cookie_consent(page, logger)
                else:
                    page = adopted2

            # ── Pre-submission checkpoint ─────────────────────────────────
            # Re-extract the page so we judge from the CURRENT browser state
            # (the user may have just finished fields manually during a
            # handoff — the old ax_tree would be stale).
            try:
                ax_tree = extractor.extract()
            except Exception:
                pass

            # ── Aggregate all "form not ready" signals ────────────────────
            # Three independent scans so we don't silently ship an
            # incomplete form:
            #   1. AXTree-based required/empty scan.
            #   2. Live-DOM custom-dropdown "Select…" placeholder scan
            #      (catches React comboboxes that AXTree misses).
            #   3. Visible client-side validation error messages.
            required_missing: list[str] = []
            for field in ax_tree.form_fields:
                if field.get("required") or "*" in field.get("name", ""):
                    val = field.get("value")
                    role = (field.get("role") or "").lower()
                    checked = field.get("checked")
                    if role in ("checkbox", "radio", "switch"):
                        if not (
                            checked is True
                            or (
                                isinstance(checked, str)
                                and checked.lower() in ("true", "on", "yes", "1")
                            )
                        ):
                            required_missing.append(field.get("name", "unknown"))
                    else:
                        if not val or not str(val).strip():
                            required_missing.append(field.get("name", "unknown"))

            try:
                empty_dropdowns = _unselected_required_dropdowns(page)
            except Exception:
                empty_dropdowns = []
            try:
                visible_errors = _live_validation_errors(page)
            except Exception:
                visible_errors = []

            blockers: list[str] = []
            blockers.extend(required_missing)
            for lbl in empty_dropdowns:
                if lbl not in blockers:
                    blockers.append(lbl)

            # Up to TWO handoff rounds — ask once, re-verify, ask again if
            # the human hasn't resolved everything.  Never auto-submit
            # a form that still has validation errors.
            for round_idx in range(2):
                if not blockers and not visible_errors:
                    break

                preview_parts: list[str] = []
                if blockers:
                    pv = ", ".join(blockers[:6])
                    if len(blockers) > 6:
                        pv += " …"
                    preview_parts.append(f"{len(blockers)} empty required: {pv}")
                if visible_errors:
                    ev = "; ".join(visible_errors[:3])
                    if len(visible_errors) > 3:
                        ev += " …"
                    preview_parts.append(
                        f"{len(visible_errors)} validation error(s): {ev}"
                    )
                preview = " | ".join(preview_parts)

                agent.show_warning(
                    f"Cannot submit yet — {preview}"
                )
                handoff = self._handoff_human_in_loop(
                    agent,
                    logger,
                    state,
                    reason=(
                        "The form isn't ready to submit. "
                        f"{preview}. "
                        "Please finish the remaining fields — "
                        "especially any dropdowns showing 'Select…' — "
                        "directly in the browser."
                    ),
                    hint=(
                        "Look for red 'This field is required.' messages "
                        "and any unfilled dropdowns, then press ENTER."
                    ),
                )
                if self._handoff_skipped_job(handoff, agent, logger, ExecutionPhase.LLM):
                    logger.log_phase_end(ExecutionPhase.LLM, False)
                    return False
                if handoff.cancelled:
                    logger.log_phase_end(ExecutionPhase.LLM, False)
                    return False
                page = handoff.page
                agent.page = page

                # Re-extract everything from the latest browser state.
                try:
                    ax_tree = extractor.extract()
                except Exception:
                    pass
                required_missing = []
                for field in ax_tree.form_fields:
                    if field.get("required") or "*" in field.get("name", ""):
                        val = field.get("value")
                        role = (field.get("role") or "").lower()
                        checked = field.get("checked")
                        if role in ("checkbox", "radio", "switch"):
                            if not (
                                checked is True
                                or (
                                    isinstance(checked, str)
                                    and checked.lower() in ("true", "on", "yes", "1")
                                )
                            ):
                                required_missing.append(
                                    field.get("name", "unknown")
                                )
                        else:
                            if not val or not str(val).strip():
                                required_missing.append(
                                    field.get("name", "unknown")
                                )
                try:
                    empty_dropdowns = _unselected_required_dropdowns(page)
                except Exception:
                    empty_dropdowns = []
                try:
                    visible_errors = _live_validation_errors(page)
                except Exception:
                    visible_errors = []
                blockers = list(required_missing)
                for lbl in empty_dropdowns:
                    if lbl not in blockers:
                        blockers.append(lbl)

            # If after two rounds the form still isn't ready, refuse to
            # submit rather than risk shipping a half-filled application.
            if blockers or visible_errors:
                summary_parts: list[str] = []
                if blockers:
                    summary_parts.append(
                        f"{len(blockers)} empty required field(s)"
                    )
                if visible_errors:
                    summary_parts.append(
                        f"{len(visible_errors)} validation error(s)"
                    )
                summary = " and ".join(summary_parts)
                agent.show_error(
                    f"Not submitting — the form still has {summary}. "
                    "Finish it in the browser and re-run JobCLI to continue."
                )
                logger.log_phase_end(ExecutionPhase.LLM, False)
                return False

            # ── Compulsory pre-submit human review (Phase 4/4) ────────────
            # The form is "ready" from the agent's perspective — every
            # signal we have says it can submit. But the user has asked
            # for a mandatory human review before any application leaves
            # the browser, in EVERY interaction mode including AUTO.
            # _handoff_human_in_loop is invoked with force_block=True so
            # the AUTO short-circuit in handoff_to_human is bypassed.
            self._form_ready_for_submit = True

            # Snapshot pre-submit state BEFORE the handoff so the
            # post-handoff confirmation detector compares against the
            # form the agent left, not the form after the human touched
            # it. URL change and submit-button disappearance are the two
            # most reliable signals that the form was accepted.
            try:
                _pre_submit_url = page.url or ""
            except Exception:
                _pre_submit_url = ""
            try:
                _pre_submit_had_submit_btn = bool(_submit_button_visible(page))
            except Exception:
                _pre_submit_had_submit_btn = True

            review_handoff = self._handoff_human_in_loop(
                agent, logger, state,
                reason=(
                    "Final review — check the form in the browser. "
                    "Press ENTER to submit this application."
                ),
                hint=(
                    "Type skip + ENTER to skip this job and open the next one. "
                    "Type cancel + ENTER to abort. "
                    "If you already submitted in the browser, JobCLI detects "
                    "the confirmation page automatically."
                ),
                force_block=True,
                submission_checker=lambda: self._looks_like_confirmation(
                    agent.page, _pre_submit_url, _pre_submit_had_submit_btn
                ),
            )
            if self._handoff_skipped_job(review_handoff, agent, logger, ExecutionPhase.LLM):
                logger.log_phase_end(ExecutionPhase.LLM, False)
                return False
            if review_handoff.cancelled:
                agent.show_warning("Submission cancelled by user during review.")
                self._submit_declined_by_user = True
                logger.log_phase_end(ExecutionPhase.LLM, False)
                return False
            page = review_handoff.page
            agent.page = page

            # ── Detect: did the user click Submit themselves? ─────────────
            # If the page already looks like a confirmation (URL changed
            # to a thank-you path, text shows "Application received",
            # submit button vanished without validation errors, …), the
            # human already did the work. Skip our own click entirely —
            # otherwise we'd hammer a now-disabled button or fall into
            # the "couldn't find Submit" recovery handoff.
            submit_clicked = False
            manual_submit_attempted = False
            user_submitted_during_review = False
            _strong_pre, _soft_pre, _pre_signals = self._looks_like_confirmation(
                page, _pre_submit_url, _pre_submit_had_submit_btn
            )
            if _strong_pre or _soft_pre:
                user_submitted_during_review = True
                manual_submit_attempted = True
                submit_clicked = True
                agent.show_success(
                    "Detected manual submission — recording success."
                )
                logger.info(
                    f"human_submitted_directly signals={_pre_signals} "
                    f"strong={_strong_pre} soft={_soft_pre}",
                    phase=ExecutionPhase.LLM,
                )

            # ── Click the Submit button ───────────────────────────────────
            # The agent fill loop never emits a "SUBMIT" action itself —
            # the Next/Submit/Apply click detection in ``_split_off_advance_clicks``
            # holds those back while required fields are still empty.
            # If the user did NOT already submit during the review, the
            # agent clicks Submit now using the ATS-specific handler
            # first (Greenhouse's ``#submit_app_button``, Ashby's
            # ``button[type=submit]``, Workday's sequence, etc.), then
            # a generic Submit locator if that fails.
            try:
                if not user_submitted_during_review and rules_handler is not None:
                    agent.show_status(
                        "Clicking submit…", phase=ExecutionPhase.LLM
                    )
                    submit_clicked = bool(rules_handler.submit_application())
            except Exception as e:
                logger.warning(
                    f"ATS-specific submit_application() raised {e!r}; "
                    "falling back to generic submit.",
                    phase=ExecutionPhase.LLM,
                )

            if not submit_clicked:
                # Generic fallback — matches the most common submit-button
                # shapes across every ATS we've seen.
                submit_candidates = [
                    "button[type='submit']:not([disabled])",
                    "input[type='submit']:not([disabled])",
                    "button:has-text('Submit Application')",
                    "button:has-text('Submit application')",
                    "button:has-text('Submit'):not([disabled])",
                    "button:has-text('Apply Now'):not([disabled])",
                    "button:has-text('Send Application')",
                    "[role='button']:has-text('Submit')",
                ]
                for sel in submit_candidates:
                    try:
                        btn = page.locator(sel).last
                        if btn.count() == 0:
                            continue
                        try:
                            btn.scroll_into_view_if_needed(timeout=1500)
                        except Exception:
                            pass
                        if not btn.is_visible(timeout=1200):
                            continue
                        btn.click(timeout=5000)
                        submit_clicked = True
                        logger.info(
                            f"Clicked submit via generic selector '{sel}'.",
                            phase=ExecutionPhase.LLM,
                        )
                        break
                    except Exception as e:
                        logger.debug(
                            f"Submit selector '{sel}' failed: {e}",
                            phase=ExecutionPhase.LLM,
                        )
                        continue

            if not submit_clicked:
                agent.show_error(
                    "Could not find the Submit button. Please click it "
                    "manually in the browser."
                )
                handoff = agent.handoff_to_human(
                    reason=(
                        "Automation couldn't locate the Submit button. "
                        "Please click Submit yourself."
                    ),
                    hint=(
                        "Click the Submit/Apply button in the browser. "
                        "When you see the confirmation page, press ENTER."
                    ),
                )
                if self._handoff_skipped_job(handoff, agent, logger, ExecutionPhase.LLM):
                    logger.log_phase_end(ExecutionPhase.LLM, False)
                    return False
                if handoff.cancelled:
                    logger.log_phase_end(ExecutionPhase.LLM, False)
                    return False
                manual_submit_attempted = True
                page = handoff.page
                agent.page = page

            # Wait for the submission to settle (navigation / SPA update).
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                try:
                    page.wait_for_timeout(2500)
                except Exception:
                    pass

            # ── Final success evaluation ──────────────────────────────────
            # Ashby / Greenhouse / Lever rarely show the same literal
            # text on their confirmation pages, and modern ATSes are
            # SPAs that often keep the URL path identical — so a fixed
            # phrase list is always incomplete. _looks_like_confirmation
            # gathers four independent signals and accepts the
            # submission as soon as one strong signal fires OR a soft
            # signal fires while no validation error is visible. The
            # same helper is used during the pre-submit handoff to
            # detect "user already submitted in the browser".
            _strong_confirm, _soft_confirm, _post_signals = self._looks_like_confirmation(
                page, _pre_submit_url, _pre_submit_had_submit_btn
            )
            _has_errors = bool(_post_signals.get("has_errors"))

            is_confirmation = _strong_confirm or _soft_confirm
            submit_attempted = submit_clicked or manual_submit_attempted
            success = bool(submit_attempted and is_confirmation)

            if success:
                agent.show_success("Application submitted!")
                page.wait_for_timeout(1000)
                logger.capture_screenshot(page, "llm_success", ExecutionPhase.LLM)
                if not _strong_confirm:
                    # Be transparent that we inferred success from
                    # behavioural signals rather than explicit text.
                    logger.info(
                        "Submission inferred from URL/form change "
                        f"(url_changed={_post_signals.get('url_changed')}, "
                        f"form_gone={_post_signals.get('form_disappeared')}, "
                        f"errors={_has_errors}).",
                        phase=ExecutionPhase.LLM,
                    )
            elif submit_attempted and not _has_errors:
                # Submit was attempted, nothing changed visibly, but no
                # error either.  Treat as probable-success so we don't
                # drop the user into a "form incomplete" handoff for a
                # form that was in fact already submitted.  The user
                # can still verify in the visible browser window.
                agent.show_success(
                    "Submit attempted — confirmation not auto-detected. "
                    "Verify in the browser; the application likely went through."
                )
                logger.capture_screenshot(
                    page, "llm_submit_unverified", ExecutionPhase.LLM
                )
                success = True
            elif submit_attempted and _has_errors:
                # Real failure: the form is still showing validation
                # errors after the click.  Fall through to the handoff
                # so the human can fix and retry.
                agent.show_warning(
                    "Submit was attempted but the form still shows validation errors. "
                    "Please fix them in the browser."
                )
                logger.capture_screenshot(
                    page, "llm_submit_failed", ExecutionPhase.LLM
                )
            else:
                agent.show_error("Submission could not be verified.")

            logger.log_phase_end(ExecutionPhase.LLM, success)
            return success

        except (InterruptedError, KeyboardInterrupt):
            raise
        except Error as e:
            if "closed" in str(e).lower():
                agent.show_error("Browser was closed — saving logs and syncing analytics.")
                logger.error("Browser closed during agent loop.", phase=ExecutionPhase.LLM)
                logger.log_phase_end(ExecutionPhase.LLM, False)
                raise BrowserClosed("User closed the browser during LLM autofill") from e
            logger.error(f"Playwright error in agent loop: {e}", phase=ExecutionPhase.LLM)
            logger.log_phase_end(ExecutionPhase.LLM, False)
            return False
        except Exception as e:
            logger.error(f"Agent loop failed: {e}", phase=ExecutionPhase.LLM)
            logger.log_phase_end(ExecutionPhase.LLM, False)
            return False

    def _navigate_job_board_via_llm(
        self,
        page: Page,
        agent: AgentInterface,
        logger: JobLogger,
        board: str,
    ) -> Optional[Page]:
        """Try to find and click the 'Apply on company site' button using the LLM."""
        try:
            page_ids_before = {id(p) for p in page.context.pages}
            url_before = page.url or ""

            # Extract AX Tree
            from jobcli.llm.ax_tree_extractor import AccessibilityTreeExtractor
            extractor = AccessibilityTreeExtractor(page)
            ax_tree = extractor.extract()

            # Ask LLM to find the button
            prompt = (
                f"I am on {board} (a job board). I need to find the button that leads to the "
                "EMPLOYER'S REAL APPLICATION PAGE on their own website. \n"
                "Common labels: 'Apply on company site', 'Apply', 'External Apply'. \n"
                "DO NOT choose 'Easy Apply' or buttons that look like LinkedIn internal applications. \n"
                "Examine the page and emit a single 'click' action for the correct button."
            )
            
            client = self._get_llm_client(logger)
            if not client:
                logger.error("No LLM client available for job board navigation", phase=ExecutionPhase.LLM)
                return None
                
            response = client.analyze_page_from_axtree(
                ax_tree, 
                self.resume, 
                task="find_apply_button"
            )

            if response and response.actions:
                # Execute the first click action
                for action in response.actions:
                    if action.action == ActionType.CLICK:
                        logger.info(f"LLM found navigation button: {action.field_label or action.selector}", phase=ExecutionPhase.LLM)
                        page.click(action.selector, timeout=5000)
                        
                        # Wait for new page/tab
                        new_page = adopt_application_page_after_action(
                            page,
                            page_count_before=len(page.context.pages),
                            url_before=url_before,
                            page_ids_before=page_ids_before,
                            logger=logger,
                            poll_seconds=5.0
                        )
                        
                        if new_page and _safe_domain(new_page.url) != _safe_domain(url_before):
                            logger.info(f"LLM successfully navigated to: {new_page.url}", phase=ExecutionPhase.LLM)
                            return new_page
            
            return None
        except Exception as e:
            logger.warning(f"LLM JobBoard navigation failed: {e}")
            return None

    # NOTE: _phase_human has been removed.

    def _scrape_browser_state_to_memory(self, page: Page, state: ApplicationState, agent: AgentInterface) -> None:
        """Extract live values from the browser and save them to AgentMemory.
        
        This ensures the agent 'learns' even from manual work done by the human
        during a handoff, not just from terminal prompts.
        """
        try:
            # 1. Grab all non-empty, non-password field values
            live_vals = page.evaluate("""
                () => {
                    const res = [];
                    document.querySelectorAll('input, select, textarea').forEach(el => {
                        if (el.type === 'password' || el.type === 'hidden' || el.type === 'file') return;
                        let val = el.value;
                        if (el.tagName === 'SELECT' && el.selectedIndex >= 0) {
                            val = el.options[el.selectedIndex].text;
                        }
                        const isPlaceholder = ["select", "choose", "--"].some(p => val.toLowerCase().includes(p));
                        if (val && val.trim() !== '' && !isPlaceholder) {
                             const label = el.getAttribute('aria-label') || el.name || el.id || '';
                             if (label && val.length < 200) {
                                 res.push({ label: label, value: val.trim() });
                             }
                        }
                    });
                    return res;
                }
            """)
            if not live_vals:
                return

            # 2. Persist to memory
            from jobcli.intelligence.memory import AgentMemory
            mem = AgentMemory(self.session, job_id=state.job_id)
            ats = state.detected_ats or ATSType.UNKNOWN
            
            learned_count = 0
            for item in live_vals:
                if mem.save_field_answer(item['label'], item['value'], ats, source="human_observed"):
                    learned_count += 1
            
            if learned_count > 0:
                agent.show_status(f"Learned {learned_count} new field(s) from your browser activity.", phase=ExecutionPhase.HUMAN)
        except Exception:
            pass

    def _dismiss_cookie_consent(self, page: Page, logger: JobLogger) -> None:
        """Dismiss cookie banners, privacy dialogs, and other overlays that block clicks."""
        from jobcli.ats.locators.overlay_dismiss import dismiss_blocking_overlays

        dismiss_blocking_overlays(page, logger, phase=ExecutionPhase.RULES)

    def _random_delay(self) -> None:
        """Add random delay using the anti-bot manager."""
        if hasattr(self, "anti_bot") and self.anti_bot:
            self.anti_bot.random_delay()
        else:
            delay = random.uniform(
                self.config.random_delay_min,
                self.config.random_delay_max,
            )
            time.sleep(delay)
