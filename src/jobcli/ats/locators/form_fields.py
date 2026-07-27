from jobcli.orchestration.human_interaction import humanized_fill
from jobcli.utils.fill_guard import should_skip_refill
"""Rule-based locators for form fields with confidence scoring.

Ported intelligence from genericStrategy.js (Chrome extension):
  - FieldConfidenceScorer: weighted keyword scoring across HTML attributes
  - should_skip_input():   filter out search bars, EEO dropdowns, AG-Grid filters
  - FIELD_KEYWORDS:        keyword registry per resume field path
  - FormFieldLocator:      confidence-ranked selector search + fuzzy dropdown fill
  - FormFiller:            orchestrates all field filling with generic fallback
"""

import re
from typing import Any, Optional, Union

from playwright.sync_api import Frame, Page

from jobcli.utils.logger import JobLogger
from jobcli.profile.schemas import ExecutionPhase, ResumeData



# ---------------------------------------------------------------------------
# EEO / search skip patterns — ported from shouldSkipInput() in extension
# ---------------------------------------------------------------------------
_EEO_PATTERN = re.compile(
    r"veteran|gender|ethnic|race|disability|sponsor|"
    r"authorized\s*to\s*work|legally\s*permitted|country\s*of\s*citizenship|"
    r"pronoun|sexual\s*orientation|marital|dependents|religion|eeo|"
    r"demographic|voluntary\s*self|protected\s*veteran",
    re.IGNORECASE,
)
_SEARCH_PLACEHOLDER = re.compile(r"^search(\.\.\.|\s*jobs?)?$", re.IGNORECASE)
_AG_GRID_ID = re.compile(r"^ag-\d+-(input|filter)$")


def should_skip_input(
    *,
    label: str = "",
    name: str = "",
    id_: str = "",
    aria: str = "",
    input_type: str = "text",
    placeholder: str = "",
    aria_hidden: bool = False,
) -> bool:
    """Return True if this input element should be skipped during autofill.

    Ported from shouldSkipInput() in genericStrategy.js.
    Skips: hidden inputs, search bars, AG-Grid filters, EEO demographic fields.
    """
    if aria_hidden:
        return True
    if input_type == "search":
        return True
    if _SEARCH_PLACEHOLDER.match(placeholder.strip()):
        return True
    if _AG_GRID_ID.match(id_):
        return True
    if "ag-" in id_ and ("filter" in id_ or "-input" in id_):
        return True

    # EEO check — only applies to selects, radios, checkboxes, comboboxes
    if input_type in ("select", "radio", "checkbox", "combobox"):
        combined = f"{label} {id_} {name} {aria}".lower()
        if _EEO_PATTERN.search(combined):
            return True

    return False


# ---------------------------------------------------------------------------
# FIELD_KEYWORDS — keyword registry per resume field path
# Drives the confidence scorer; ported from fieldSynonyms.js patterns
# ---------------------------------------------------------------------------
FIELD_KEYWORDS: dict[str, list[str]] = {
    "personal.first_name": [
        "first_name", "firstname", "fname", "given_name", "givenname",
        "first name", "given name", "legalname first", "legal first",
    ],
    "personal.last_name": [
        "last_name", "lastname", "lname", "surname", "family_name", "familyname",
        "last name", "family name", "legalname last", "legal last",
    ],
    "personal.email": [
        "email", "e-mail", "mail", "email address", "emailaddress",
    ],
    "personal.phone": [
        "phone", "tel", "mobile", "cell", "sms", "telephone",
        "phone number", "contact number", "mobile number", "phonenumber",
    ],
    "personal.address": [
        "address", "street", "street address", "address line 1",
        "address_line1", "mailing address", "addressline1",
    ],
    "personal.city": [
        "city", "town", "locality", "municipality",
    ],
    "personal.state": [
        "state", "province", "region", "territory",
        "statecode", "state_code", "state/province",
    ],
    "personal.country": [
        "country", "countrycode", "country_code", "nation", "country name",
    ],
    "personal.zip_code": [
        "zip", "zip_code", "zipcode", "postal", "postal_code", "postcode",
        "zip code", "postal code",
    ],
    "personal.linkedin": [
        "linkedin", "linkedin url", "linkedin profile", "linked in",
        "linkedinurl", "linkedin profile url",
    ],
    "personal.github": [
        "github", "github url", "github profile", "githuburl",
    ],
    "personal.portfolio": [
        "portfolio", "personal website", "portfolio url",
        "personal_website", "company_site", "portfoliourl",
    ],
    "personal.website": [
        "website", "web site", "personal site", "homepage", "url",
    ],
}


# ---------------------------------------------------------------------------
# FieldConfidenceScorer
# Ported from calculateConfidence() + resolveFieldFromHtmlSemantics()
# ---------------------------------------------------------------------------
class FieldConfidenceScorer:
    """Score how well an HTML element matches a resume field (0–100).

    Ported from calculateConfidence() and resolveFieldFromHtmlSemantics()
    in genericStrategy.js.
    """

    # Per-attribute weights (from extension source)
    WEIGHTS: dict[str, int] = {
        "name":        40,
        "id":          40,
        "aria_label":  35,
        "label_text":  35,
        "placeholder": 20,
    }

    # HTML autocomplete → resume field path — returns confidence 99
    AUTOCOMPLETE_MAP: dict[str, str] = {
        "given-name":     "personal.first_name",
        "family-name":    "personal.last_name",
        "email":          "personal.email",
        "tel":            "personal.phone",
        "tel-national":   "personal.phone",
        "tel-local":      "personal.phone",
        "street-address": "personal.address",
        "address-line1":  "personal.address",
        "address-level2": "personal.city",
        "address-level1": "personal.state",
        "postal-code":    "personal.zip_code",
        "country":        "personal.country",
        "country-name":   "personal.country",
        "url":            "personal.linkedin",
    }

    def score(
        self,
        features: dict[str, str],
        keywords: list[str],
        field_key: str,
    ) -> int:
        """Return 0–100 confidence that this element matches field_key."""
        keyword_score = 0
        matched_primary = False

        for kw in keywords:
            kw_lower = kw.lower()
            for attr, weight in self.WEIGHTS.items():
                attr_value = features.get(attr, "")
                if attr_value and kw_lower in attr_value:
                    keyword_score += weight
                    matched_primary = True
                    if attr_value == kw_lower:  # exact match bonus
                        keyword_score += weight // 2

        keyword_score = min(keyword_score, 70)

        # Nearby text context bonus (≥8-char keywords only, capped at 15)
        context_score = 0
        nearby = features.get("nearby_text", "")
        if nearby:
            for kw in keywords:
                if len(kw) >= 8 and kw.lower() in nearby:
                    context_score += 5
        context_score = min(context_score, 15)

        # Input-type alignment bonus
        input_type = features.get("input_type", "text")
        is_email = field_key == "personal.email" or field_key.endswith(".email")
        is_phone = field_key == "personal.phone" or field_key.endswith(".phone")
        is_url = any(
            x in field_key
            for x in ("url", "linkedin", "github", "portfolio", "website")
        )
        if is_email and input_type == "email":
            type_score = 15
        elif is_phone and input_type == "tel":
            type_score = 15
        elif is_url and input_type == "url":
            type_score = 15
        else:
            type_score = 5

        confidence = keyword_score + context_score + type_score
        if not matched_primary:
            confidence = min(confidence, 30)

        return min(round(confidence), 100)

    @staticmethod
    def resolve_from_resume(path: str, resume: ResumeData) -> Optional[str]:
        """Follow a dotted resume path and return the string value if set."""
        parts = path.split(".")
        obj: Any = resume
        for part in parts:
            if obj is None:
                return None
            if hasattr(obj, part):
                obj = getattr(obj, part)
            elif isinstance(obj, dict) and part in obj:
                obj = obj[part]
            else:
                return None
        return str(obj) if obj is not None else None

    def resolve_from_autocomplete(
        self, autocomplete: str, resume: ResumeData, field_key: str
    ) -> Optional[tuple[str, int]]:
        """Return (value, confidence=99) IF the autocomplete matches field_key."""
        ac = autocomplete.lower().strip()
        path = self.AUTOCOMPLETE_MAP.get(ac)
        if not path or path != field_key:
            return None
        value = self.resolve_from_resume(path, resume)
        return (value, 99) if value else None


# ---------------------------------------------------------------------------
# FormFieldLocator — confidence-ranked field detection
# ---------------------------------------------------------------------------
_COLLECT_FEATURES_JS = """
(tag) => {
    const elements = document.querySelectorAll(
        tag + ':not([type="hidden"]):not([disabled])'
    );
    return Array.from(elements).map(el => {
        // Resolve label text
        let labelText = '';
        try {
            if (el.parentElement && el.parentElement.tagName === 'LABEL') {
                labelText = el.parentElement.innerText || '';
            } else if (el.id) {
                const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
                if (lbl) labelText = lbl.innerText || '';
            }
            if (!labelText) {
                const lb = (el.getAttribute('aria-labelledby') || '').trim();
                if (lb) {
                    labelText = lb.split(/\\s+/)
                        .map(id => { const e = document.getElementById(id); return e ? e.innerText : ''; })
                        .filter(Boolean).join(' ');
                }
            }
            if (!labelText) {
                labelText = el.getAttribute('aria-label') || el.getAttribute('placeholder') || '';
            }
        } catch(_) {}

        // Nearby text (2 parent hops)
        let nearbyText = '';
        try {
            let c = el.parentElement;
            for (let i = 0; i < 2 && c; i++, c = c.parentElement) {
                const t = c.innerText || '';
                if (t.length > 0 && t.length < 200) { nearbyText = t.toLowerCase(); break; }
            }
        } catch(_) {}

        const id   = (el.id   || '').toLowerCase();
        const name = (el.name || '').toLowerCase();
        let selector = '';
        if (id)   selector = '#' + id;
        else if (name) selector = el.tagName.toLowerCase() + '[name="' + name + '"]';

        const rect = el.getBoundingClientRect();
        const isVisible = rect.width > 1 && rect.height > 1;

        return {
            selector,
            name:         name,
            id:           id,
            aria_label:   (el.getAttribute('aria-label')   || '').toLowerCase(),
            placeholder:  (el.getAttribute('placeholder')  || '').toLowerCase(),
            autocomplete: (el.getAttribute('autocomplete') || '').toLowerCase(),
            input_type:   (el.type || (el.tagName === 'SELECT' ? 'select' : 'text')).toLowerCase(),
            aria_hidden:  el.getAttribute('aria-hidden') === 'true',
            tag:          el.tagName.toLowerCase(),
            label_text:   labelText.toLowerCase(),
            nearby_text:  nearbyText,
            is_visible:   isVisible,
        };
    }).filter(f => f.selector && f.is_visible);
}
"""


class FormFieldLocator:
    """Locator for form fields using confidence-ranked DOM scanning.

    New methods:
      find_best_selector()         — scored, returns (selector, confidence) or None
      fill_field_with_confidence() — end-to-end field fill using scored detection
      fill_select_fuzzy()          — fuzzy dropdown match (replaces exact select_option)

    Legacy methods kept for backward compatibility:
      find_field_by_label(), fill_text_field(), fill_select_field(), upload_file()
    """

    CONFIDENCE_THRESHOLD = 70   # minimum score to auto-fill
    MIN_PROMPT_CONFIDENCE = 52  # below this: don't even try

    def __init__(self, page: Union[Page, Frame], logger: Optional[JobLogger] = None) -> None:
        """Initialize field locator (top-level page or embedded apply iframe)."""
        self.page = page
        self.logger = logger
        self._scorer = FieldConfidenceScorer()

    # ------------------------------------------------------------------
    # Core: collect all form element features in a single JS round-trip
    # ------------------------------------------------------------------
    def _collect_all_form_features(self, field_type: str = "input") -> list[dict]:
        """Run one JS evaluate to get features for all matching elements."""
        try:
            return self.page.evaluate(_COLLECT_FEATURES_JS, field_type) or []
        except Exception as e:
            if self.logger:
                self.logger.warning(
                    f"Failed to collect form features: {e}",
                    phase=ExecutionPhase.RULES,
                )
            return []

    # ------------------------------------------------------------------
    # Confidence-ranked selector finder
    # ------------------------------------------------------------------
    def find_best_selector(
        self,
        field_key: str,
        resume: ResumeData,
        field_type: str = "input",
    ) -> Optional[tuple[str, int]]:
        """Return (css_selector, confidence) for the best-matching element.

        Strategy:
          1. Collect all visible field_type elements in one JS call
          2. For each: try autocomplete fast-path (conf=99)
          3. Otherwise: score against FIELD_KEYWORDS[field_key]
          4. Return highest-confidence selector ≥ CONFIDENCE_THRESHOLD
        """
        keywords = FIELD_KEYWORDS.get(field_key, [])
        if not keywords:
            return None

        features_list = self._collect_all_form_features(field_type)
        best_selector: Optional[str] = None
        best_confidence = 0

        for features in features_list:
            try:
                if should_skip_input(
                    label=features.get("label_text", ""),
                    name=features.get("name", ""),
                    id_=features.get("id", ""),
                    aria=features.get("aria_label", ""),
                    input_type=features.get("input_type", "text"),
                    placeholder=features.get("placeholder", ""),
                    aria_hidden=features.get("aria_hidden", False),
                ):
                    continue

                sel = features.get("selector", "")
                if not sel:
                    continue

                # --- Autocomplete fast-path (highest priority) ---
                ac = features.get("autocomplete", "")
                if ac:
                    result = self._scorer.resolve_from_autocomplete(ac, resume, field_key)
                    if result:
                        _, confidence = result
                        if confidence > best_confidence:
                            best_confidence = confidence
                            best_selector = sel
                        continue  # don't also score by keywords

                # --- Keyword confidence scoring ---
                confidence = self._scorer.score(features, keywords, field_key)
                if confidence > best_confidence:
                    best_confidence = confidence
                    best_selector = sel

            except Exception:
                continue

        if best_confidence >= self.CONFIDENCE_THRESHOLD and best_selector:
            if self.logger:
                self.logger.info(
                    f"Matched '{field_key}' -> '{best_selector}' "
                    f"(confidence {best_confidence})",
                    phase=ExecutionPhase.RULES,
                )
            return (best_selector, best_confidence)

        if self.logger:
            self.logger.info(
                f"No confident match for '{field_key}' "
                f"(best={best_confidence}, threshold={self.CONFIDENCE_THRESHOLD})",
                phase=ExecutionPhase.RULES,
            )
        return None

    # ------------------------------------------------------------------
    # Confidence-ranked fill
    # ------------------------------------------------------------------
    def fill_field_with_confidence(
        self,
        field_key: str,
        value: str,
        resume: ResumeData,
    ) -> bool:
        """Find the best-matching field for field_key and fill it."""
        result = self.find_best_selector(field_key, resume, field_type="input")
        if not result:
            result = self.find_best_selector(field_key, resume, field_type="textarea")

        if result:
            selector, confidence = result
            try:
                humanized_fill(self.page, self.page.locator(selector).first, value)
                if self.logger:
                    self.logger.info(
                        f"Filled '{field_key}' via confidence scoring ({confidence})",
                        phase=ExecutionPhase.RULES,
                        selector=selector,
                        value=value[:60],
                    )
                return True
            except Exception as e:
                if self.logger:
                    self.logger.warning(
                        f"fill_field_with_confidence failed for '{field_key}': {e}",
                        phase=ExecutionPhase.RULES,
                        selector=selector,
                    )

        return False

    # ------------------------------------------------------------------
    # Fuzzy dropdown fill
    # ------------------------------------------------------------------
    def fill_select_fuzzy(self, selector: str, value: str) -> bool:
        """Select a <select> option by fuzzy text match.

        Tries in order:
          1. Exact value or text match (case-insensitive)
          2. Option text contains the desired value
          3. Desired value contains the option text
        """
        try:
            element = self.page.query_selector(selector)
            if not element:
                if self.logger:
                    self.logger.warning(
                        f"fill_select_fuzzy: element not found for '{selector}'",
                        phase=ExecutionPhase.RULES,
                    )
                return False

            options: list[dict] = element.evaluate(
                "el => Array.from(el.options).map(o => ({value: o.value, text: o.text.trim()}))"
            )

            value_lower = value.lower()
            chosen_value: Optional[str] = None

            # Pass 1 — exact
            for opt in options:
                if (
                    opt["value"].lower() == value_lower
                    or opt["text"].lower() == value_lower
                ):
                    chosen_value = opt["value"]
                    break

            # Pass 2 — option text contains desired value
            if chosen_value is None:
                for opt in options:
                    if value_lower in opt["text"].lower():
                        chosen_value = opt["value"]
                        break

            # Pass 3 — desired value contains option text (short option labels)
            if chosen_value is None:
                for opt in options:
                    opt_lower = opt["text"].lower()
                    if opt_lower and len(opt_lower) > 1 and opt_lower in value_lower:
                        chosen_value = opt["value"]
                        break

            if chosen_value is not None:
                self.page.select_option(selector, value=chosen_value, timeout=3000)
                if self.logger:
                    self.logger.info(
                        f"Fuzzy-selected '{value}' on '{selector}'",
                        phase=ExecutionPhase.RULES,
                    )
                return True

            if self.logger:
                self.logger.warning(
                    f"No matching option for '{value}' in '{selector}'",
                    phase=ExecutionPhase.RULES,
                    available_options=[o["text"] for o in options[:8]],
                )
            return False

        except Exception as e:
            if self.logger:
                self.logger.warning(
                    f"fill_select_fuzzy failed on '{selector}': {e}",
                    phase=ExecutionPhase.RULES,
                )
            return False
    def fill_radio_field(self, labels: list[str], value: str) -> bool:
        """Select a radio button by matching its label text."""

        value_lower = str(value).strip().lower()

        try:
            radios = self.page.locator("input[type='radio']")

            for i in range(radios.count()):
                radio = radios.nth(i)

                try:
                    radio_id = radio.get_attribute("id") or ""
                    radio_value = (radio.get_attribute("value") or "").strip().lower()

                    label_text = ""
                    if radio_id:
                        lbl = self.page.locator(f"label[for='{radio_id}']")
                        if lbl.count():
                            label_text = lbl.first.inner_text().strip().lower()

                    if (
                        value_lower == radio_value
                        or value_lower == label_text
                        or value_lower in label_text
                    ):
                        radio.check(force=True)

                        if self.logger:
                            self.logger.info(
                                f"Selected radio option '{value}'",
                                phase=ExecutionPhase.RULES,
                            )

                        return True

                except Exception:
                    continue

        except Exception as e:
            if self.logger:
                self.logger.warning(
                    f"Failed to fill radio field: {e}",
                    phase=ExecutionPhase.RULES,
                )

        return False

    def fill_checkbox_field(self, labels: list[str], value: str) -> bool:
        """Check or uncheck a checkbox."""

        should_check = str(value).strip().lower() in {
            "yes",
            "true",
            "1",
            "checked",
            "accept",
        }

        try:
            checkboxes = self.page.locator("input[type='checkbox']")

            for i in range(checkboxes.count()):
                checkbox = checkboxes.nth(i)

                try:
                    checkbox_id = checkbox.get_attribute("id") or ""
                    label_text = ""

                    if checkbox_id:
                        lbl = self.page.locator(f"label[for='{checkbox_id}']")
                        if lbl.count():
                            label_text = lbl.first.inner_text().strip().lower()

                    for label in labels:
                        if label.lower() in label_text:
                            if should_check:
                                checkbox.check(force=True)
                            else:
                                checkbox.uncheck(force=True)

                            if self.logger:
                                self.logger.info(
                                    f"Checkbox '{label}' updated",
                                    phase=ExecutionPhase.RULES,
                                )

                            return True

                except Exception:
                    continue

        except Exception as e:
            if self.logger:
                self.logger.warning(
                    f"Failed to fill checkbox field: {e}",
                    phase=ExecutionPhase.RULES,
                )

        return False

    def fill_multiselect_field(
        self,
        labels: list[str],
        values: list[str],
    ) -> bool:
        """Select multiple options from a multi-select element."""

        selector = self.find_field_by_label(labels, "select")
        if not selector:
            return False

        try:
            element = self.page.query_selector(selector)
            if not element:
                return False

            options = element.evaluate(
                """
                el => Array.from(el.options).map(o => ({
                    value: o.value,
                    text: o.text.trim()
                }))
                """
            )

            selected = []

            for wanted in values:
                wanted = wanted.lower()

                for opt in options:
                    if (
                        wanted == opt["text"].lower()
                        or wanted == opt["value"].lower()
                        or wanted in opt["text"].lower()
                    ):
                        selected.append(opt["value"])
                        break

            if selected:
                self.page.select_option(selector, value=selected)

                if self.logger:
                    self.logger.info(
                        f"Selected {len(selected)} multi-select options",
                        phase=ExecutionPhase.RULES,
                    )

                return True

        except Exception as e:
            if self.logger:
                self.logger.warning(
                    f"Failed to fill multi-select field: {e}",
                    phase=ExecutionPhase.RULES,
                )

        return False    
    # ------------------------------------------------------------------
    # Legacy methods — kept for backward compatibility
    # ------------------------------------------------------------------
    def find_field_by_label(
        self, labels: list[str], field_type: str = "input"
    ) -> Optional[str]:
        """Find a field by label text (legacy method kept for compatibility)."""
        for label in labels:
            try:
                selector = f"label:has-text('{label}') + {field_type}"
                if self.page.query_selector(selector):
                    return selector

                selector = f"{field_type}[placeholder*='{label}' i]"
                if self.page.query_selector(selector):
                    return selector

                name_key = label.lower().replace(" ", "_")
                selector = f"{field_type}[name*='{name_key}']"
                if self.page.query_selector(selector):
                    return selector

                selector = f"{field_type}[id*='{name_key}']"
                if self.page.query_selector(selector):
                    return selector

            except Exception:
                continue
        return None

    def fill_text_field(self, labels: list[str], value: str) -> bool:
        """Fill text field by label (legacy)."""
        selector = self.find_field_by_label(labels, "input")
        if not selector:
            selector = self.find_field_by_label(labels, "textarea")
        if selector:
            try:
                loc = self.page.locator(selector).first
                if should_skip_refill(loc, value):
                    if self.logger:
                        self.logger.info(
                            f"Skipping '{labels[0]}' — already has value",
                            phase=ExecutionPhase.RULES,
                            selector=selector,
                        )
                    return True
                humanized_fill(self.page, loc, value)
                if self.logger:
                    self.logger.info(
                        f"Filled field: {labels[0]}",
                        phase=ExecutionPhase.RULES,
                        selector=selector,
                        value=value[:50],
                    )
                return True
            except Exception as e:
                if self.logger:
                    self.logger.warning(
                        f"Failed to fill field '{labels[0]}': {e}",
                        phase=ExecutionPhase.RULES,
                    )
        return False

    def fill_select_field(self, labels: list[str], value: str) -> bool:
        """Fill select dropdown — now uses fuzzy matching (legacy entry-point)."""
        selector = self.find_field_by_label(labels, "select")
        if selector:
            return self.fill_select_fuzzy(selector, value)
        return False

    def upload_file(self, labels: list[str], file_path: str) -> bool:
        """Upload file to a file input."""
        selector = self.find_field_by_label(labels, "input[type='file']")
        if selector:
            try:
                self.page.set_input_files(selector, file_path, timeout=3000)
                if self.logger:
                    self.logger.info(
                        f"Uploaded file: {labels[0]}",
                        phase=ExecutionPhase.RULES,
                        selector=selector,
                        file_path=file_path,
                    )
                return True
            except Exception as e:
                if self.logger:
                    self.logger.warning(
                        f"Failed to upload file '{labels[0]}': {e}",
                        phase=ExecutionPhase.RULES,
                    )
        return False


# ---------------------------------------------------------------------------
# FormFiller — orchestrates field filling with confidence scoring + fallback
# ---------------------------------------------------------------------------
class FormFiller:
    """Fill job application forms using resume data.

    fill_personal_info() now uses confidence scoring as primary strategy
    and falls back to legacy label matching if confidence scoring finds nothing.
    All dropdowns use fill_select_fuzzy() instead of exact select_option().
    """

    # Legacy label lists kept for fallback path
    FIELD_LABELS: dict[str, list[str]] = {
        "first_name": ["First Name", "Given Name", "First", "Name"],
        "last_name":  ["Last Name", "Surname", "Family Name", "Last"],
        "email":      ["Email", "Email Address", "E-mail"],
        "phone":      ["Phone", "Phone Number", "Mobile", "Telephone", "Contact Number"],
        "address":    ["Address", "Street Address", "Address Line 1"],
        "city":       ["City", "Town"],
        "state":      ["State", "Province", "Region"],
        "country":    ["Country"],
        "zip_code":   ["Zip Code", "Postal Code", "ZIP", "Postcode"],
        "linkedin":   ["LinkedIn", "LinkedIn URL", "LinkedIn Profile"],
        "github":     ["GitHub", "GitHub URL", "GitHub Profile"],
        "portfolio":  ["Portfolio", "Portfolio URL", "Website"],
        "website":    ["Website", "Personal Website", "Homepage"],
    }

    def __init__(
        self, page: Union[Page, Frame], resume: ResumeData, logger: Optional[JobLogger] = None
    ) -> None:
        """Initialize form filler (page or Workday candidate iframe)."""
        self.page = page
        self.resume = resume
        self.logger = logger
        self.field_locator = FormFieldLocator(page, logger)

    def fill_personal_info(self) -> dict[str, bool]:
        """Fill personal information fields.

        Uses confidence scoring first; falls back to legacy label matching
        for any field that scores below threshold.

        For phone fields, also attempts to fill the country-code dropdown
        (selecting +1 for US/Canada) before filling the number input.
        """
        results: dict[str, bool] = {}
        personal = self.resume.personal

        # ── Phone country-code dropdown: try first, before the normal fill pass
        # Many ATS forms show a flag/country-code picker next to the phone input.
        # We handle this separately so the bare national number goes into the text
        # input and +1 is set in the dropdown, regardless of what format the
        # resume JSON stores the number in.
        if personal.phone:
            try:
                from jobcli.utils.phone_utils import fill_phone_with_country_code
                phone_ok = fill_phone_with_country_code(
                    self.page, personal.phone, self.logger
                )
                if phone_ok:
                    results["phone"] = True
                    if self.logger:
                        self.logger.info(
                            "Phone filled via country-code dropdown helper",
                            phase=ExecutionPhase.RULES,
                        )
            except Exception:
                pass

        # Map field_key (dot-path) → (short_key, value)
        field_map: list[tuple[str, str, Optional[str]]] = [
            ("personal.first_name", "first_name", personal.first_name),
            ("personal.last_name",  "last_name",  personal.last_name),
            ("personal.email",      "email",       personal.email),
            ("personal.phone",      "phone",       personal.phone),
            ("personal.address",    "address",     personal.address),
            ("personal.city",       "city",        personal.city),
            ("personal.state",      "state",       personal.state),
            ("personal.country",    "country",     personal.country),
            ("personal.zip_code",   "zip_code",    personal.zip_code),
            ("personal.linkedin",   "linkedin",    personal.linkedin),
            ("personal.github",     "github",      personal.github),
            ("personal.portfolio",  "portfolio",   personal.portfolio),
            ("personal.website",    "website",     personal.website),
        ]

        used_selectors = set()
        for field_key, short_key, value in field_map:
            if not value:
                continue

            # Phone already handled by country-code helper above — skip confidence
            # scoring to avoid overwriting the correctly-filled number input with
            # the raw resume value (which may include the +1 prefix).
            if short_key == "phone" and results.get("phone"):
                continue

            # Primary: confidence scoring
            result = self.field_locator.find_best_selector(field_key, self.resume)
            if result:
                selector, confidence = result
                if selector in used_selectors:
                    continue  # Don't overwrite 
                
                try:
                    loc = self.page.locator(selector).first
                    if should_skip_refill(loc, value):
                        if self.logger:
                            self.logger.info(
                                f"Skipping '{field_key}' — already has value",
                                phase=ExecutionPhase.RULES,
                                selector=selector,
                            )
                        results[short_key] = True
                        used_selectors.add(selector)
                        continue

                    # For phone fields: ALWAYS use bare national digits (e.g. 4155550192).
                    # Never use +14155550192 — ATS phone inputs that have a country-code
                    # dropdown/overlay already handle +1; passing the full E.164 string
                    # causes masking libraries to insert a spurious leading '0'.
                    fill_value = value
                    if short_key == "phone":
                        try:
                            from jobcli.utils.phone_utils import bare_national_number
                            fill_value = bare_national_number(value) or value
                        except Exception:
                            pass

                    humanized_fill(self.page, loc, fill_value)
                    used_selectors.add(selector)
                    results[short_key] = True
                except Exception:
                    pass

            if not results.get(short_key):
                # Fallback: legacy label matching
                labels = self.FIELD_LABELS.get(short_key, [short_key.replace("_", " ").title()])
                fill_val = value
                if short_key == "phone":
                    try:
                        from jobcli.utils.phone_utils import bare_national_number
                        fill_val = bare_national_number(value) or value
                    except Exception:
                        pass
                if classified_field == "radio":
                    success = self.field_locator.fill_radio_field(labels, fill_val)

                elif classified_field == "checkbox":
                    success = self.field_locator.fill_checkbox_field(labels, fill_val)

                elif classified_field == "multiselect":
                    values = (
                        fill_val
                        if isinstance(fill_val, list)
                        else [v.strip() for v in str(fill_val).split(",") if v.strip()]
                    )
                    success = self.field_locator.fill_multiselect_field(labels, values)

                elif classified_field == "select":
                    success = self.field_locator.fill_select_field(labels, fill_val)

                else:
                    success = self.field_locator.fill_text_field(labels, fill_val)

                results[short_key] = success

        return results

    def upload_resume(self, resume_path: str) -> bool:
        """Upload resume file."""
        labels = ["Resume", "CV", "Upload Resume", "Upload CV", "Attach Resume"]
        return self.field_locator.upload_file(labels, resume_path)

    def upload_cover_letter(self, cover_letter_path: str) -> bool:
        """Upload cover letter."""
        labels = ["Cover Letter", "Upload Cover Letter", "Attach Cover Letter"]
        return self.field_locator.upload_file(labels, cover_letter_path)

    def fill_work_authorization(self) -> dict[str, bool]:
        """Fill work authorization fields using fuzzy dropdown matching."""
        results: dict[str, bool] = {}
        auth = self.resume.work_authorization

        if auth.authorized_to_work:
            labels = [
                "Are you authorized to work",
                "Work Authorization",
                "Authorized to work",
            ]
            selector = self.field_locator.find_field_by_label(labels, "select")
            results["authorized"] = (
                self.field_locator.fill_select_fuzzy(selector, "Yes")
                if selector else False
            )

        if not auth.require_sponsorship:
            labels = [
                "Do you require sponsorship",
                "Require Sponsorship",
                "Visa Sponsorship",
            ]
            selector = self.field_locator.find_field_by_label(labels, "select")
            results["sponsorship"] = (
                self.field_locator.fill_select_fuzzy(selector, "No")
                if selector else False
            )

        return results

    def fill_demographics(self) -> dict[str, bool]:
        """Fill demographic fields (optional)."""
        results: dict[str, bool] = {}
        if not self.resume.demographics:
            return results

        demo = self.resume.demographics

        if demo.gender:
            sel = self.field_locator.find_field_by_label(["Gender"], "select")
            if sel:
                results["gender"] = self.field_locator.fill_select_fuzzy(sel, demo.gender)

        if demo.race:
            sel = self.field_locator.find_field_by_label(["Race", "Ethnicity"], "select")
            if sel:
                results["race"] = self.field_locator.fill_select_fuzzy(sel, demo.race)

        if demo.veteran_status:
            sel = self.field_locator.find_field_by_label(["Veteran Status"], "select")
            if sel:
                results["veteran"] = self.field_locator.fill_select_fuzzy(
                    sel, demo.veteran_status
                )

        if demo.disability_status:
            sel = self.field_locator.find_field_by_label(["Disability Status"], "select")
            if sel:
                results["disability"] = self.field_locator.fill_select_fuzzy(
                    sel, demo.disability_status
                )

        return results

    def fill_all(self, resume_path: Optional[str] = None) -> dict[str, Any]:
        """Fill all detected form fields."""
        results: dict[str, Any] = {}

        if self.logger:
            self.logger.info("Starting form fill", phase=ExecutionPhase.RULES)

        results["personal_info"] = self.fill_personal_info()

        if resume_path:
            results["resume_uploaded"] = self.upload_resume(resume_path)

        results["work_authorization"] = self.fill_work_authorization()
        results["demographics"] = self.fill_demographics()

        if self.logger:
            personal_results = results.get("personal_info", {})
            filled = sum(1 for v in personal_results.values() if v)
            total = len(personal_results)
            self.logger.info(
                f"Form fill complete: {filled}/{total} personal fields filled",
                phase=ExecutionPhase.RULES,
                results=results,
            )

        return results
