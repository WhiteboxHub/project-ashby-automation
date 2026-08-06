"""Ashby ATS handler."""

import re
from typing import Any, Optional

from jobcli.profile.schemas import ApplicationState, ExecutionPhase, ResumeData
from jobcli.ats.handlers.generic_handler import GenericATSHandler


# ---------------------------------------------------------------------------
# Keyword tables for rule-based Yes/No and paragraph answering
# ---------------------------------------------------------------------------

# Maps question-keyword patterns → (answer_if_true, answer_if_false, resume_attr_path)
# resume_attr_path is a dotted path into ResumeData; None means use a hardcoded default.
_YES_NO_RULES: list[tuple[re.Pattern, str, str, Optional[str]]] = [
    # Sponsorship questions → answer from work_authorization.require_sponsorship
    (
        re.compile(
            r"sponsor|visa\s+sponsor|require\s+sponsor|need\s+sponsor|"
            r"immigration\s+sponsor|sponsorship",
            re.IGNORECASE,
        ),
        "Yes",   # answer when the attribute is True
        "No",    # answer when the attribute is False (default: no sponsorship needed)
        "work_authorization.require_sponsorship",
    ),
    # Authorization / right-to-work → work_authorization.authorized_to_work
    (
        re.compile(
            r"authorized\s+to\s+work|legally\s+authorized|right\s+to\s+work|"
            r"eligible\s+to\s+work|legally\s+permitted|work\s+authorization|"
            r"are\s+you\s+authorized|are\s+you\s+legally|can\s+you\s+legally",
            re.IGNORECASE,
        ),
        "Yes",   # authorized → Yes
        "No",    # not authorized → No
        "work_authorization.authorized_to_work",
    ),
    # Currently employed / working → infer from last experience having no end_date
    (
        re.compile(
            r"currently\s+employ|currently\s+work|currently\s+at|present\s+employ",
            re.IGNORECASE,
        ),
        "Yes",
        "No",
        "_currently_employed",   # special key handled in code
    ),
    # Willing to relocate — conservative default: No
    (
        re.compile(r"relocat|willing\s+to\s+move", re.IGNORECASE),
        "Yes",
        "No",
        "_hardcoded_no",
    ),
    # Remote / work remotely — default: Yes
    (
        re.compile(r"work\s+remote|remote\s+work|open\s+to\s+remote", re.IGNORECASE),
        "Yes",
        "No",
        "_hardcoded_yes",
    ),
    # US Citizen / citizenship
    (
        re.compile(r"us\s+citizen|united\s+states\s+citizen|citizenship", re.IGNORECASE),
        "Yes",
        "No",
        "_hardcoded_yes",
    ),
    # Security clearance — default: No
    (
        re.compile(r"security\s+clearance|clearance", re.IGNORECASE),
        "Yes",
        "No",
        "_hardcoded_no",
    ),
    # Transgender identification — default: No
    (
        re.compile(r"transgender", re.IGNORECASE),
        "Yes",
        "No",
        "_hardcoded_no",
    ),
]

# Maps paragraph-question keyword patterns → how to build the answer from resume
_PARAGRAPH_RULES: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            r"how\s+did\s+you\s+know|success|look\s+like|metric|outcome|measure|verify|evaluat|how.*work",
            re.IGNORECASE,
        ),
        "outcome_experience",
    ),
    (
        re.compile(
            r"have\s+you\s+used|build|explore|project|side\s+project|personal\s+project|prototype|demo|tool|what\s+did\s+you",
            re.IGNORECASE,
        ),
        "project_experience",
    ),
    (
        re.compile(
            r"technical\s+support|log\s+analysis|support\s+issue|"
            r"troubleshoot|debug|diagnose|resolve\s+a\s+problem",
            re.IGNORECASE,
        ),
        "technical_experience",
    ),
    (
        re.compile(
            r"b2b|saas|software\s+product|product\s+support|"
            r"enterprise\s+software|technical\s+product",
            re.IGNORECASE,
        ),
        "product_experience",
    ),
    (
        re.compile(
            r"escalat|not.*obvious|difficult\s+decision|ambiguous|"
            r"judgment\s+call|when\s+to\s+escalate",
            re.IGNORECASE,
        ),
        "decision_experience",
    ),
    (
        re.compile(
            r"team|collaborat|cross.functional|work\s+with\s+others|"
            r"stakeholder|colleague",
            re.IGNORECASE,
        ),
        "collaboration_experience",
    ),
    (
        re.compile(
            r"why\s+(are\s+you|do\s+you|this|us)|motivat|interest|passion|"
            r"excited\s+about|attracted\s+to",
            re.IGNORECASE,
        ),
        "motivation",
    ),
    (
        re.compile(
            r"tell\s+us\s+about\s+yourself|background|introduce\s+yourself|"
            r"about\s+you|who\s+are\s+you",
            re.IGNORECASE,
        ),
        "background",
    ),
    (
        re.compile(
            r"experience\s+with|familiar\s+with|knowledge\s+of|proficien",
            re.IGNORECASE,
        ),
        "skills_experience",
    ),
    (
        re.compile(r"achieve|accomplish|proud|impact|result|outcome", re.IGNORECASE),
        "achievement",
    ),
]


class AshbyHandler(GenericATSHandler):
    """Handler for Ashby ATS (jobs.ashbyhq.com).

    Ashby uses clean standard HTML form attributes.
    Fields use simple name attrs: firstName, lastName, email, phone, etc.
    """

    _NAME_FIELD_MAP = [
        ("firstName",    "personal.first_name",  95),
        ("first_name",   "personal.first_name",  95),
        ("lastName",     "personal.last_name",   95),
        ("last_name",    "personal.last_name",   95),
        ("email",        "personal.email",       95),
        ("phone",        "personal.phone",       90),
        ("phoneNumber",  "personal.phone",       90),
        ("linkedin",     "personal.linkedin",    90),
        ("linkedinUrl",  "personal.linkedin",    95),
        ("github",       "personal.github",      90),
        ("githubUrl",    "personal.github",      95),
        ("website",      "personal.website",     85),
        ("portfolioUrl", "personal.portfolio",   90),
        ("city",         "personal.city",        85),
        ("address",      "personal.address",     85),
    ]

    def find_platform_specific_match(
        self, input_selector: str, resume: ResumeData
    ) -> Optional[dict]:
        try:
            el = self.page.query_selector(input_selector)
            if not el:
                return None
            name_attr = el.get_attribute("name") or ""
            from jobcli.ats.locators.form_fields import FieldConfidenceScorer
            for field_name, path, confidence in self._NAME_FIELD_MAP:
                if name_attr == field_name or name_attr.lower() == field_name.lower():
                    value = FieldConfidenceScorer.resolve_from_resume(path, resume)
                    if value:
                        return {"value": value, "confidence": confidence}
        except Exception as e:
            if self.logger:
                self.logger.warning(f"Ashby platform match error: {e}", phase=ExecutionPhase.RULES)
        return None

    def find_apply_button(self) -> bool:
        if self.logger:
            self.logger.info("Looking for Ashby apply button", phase=ExecutionPhase.RULES)

        # Check if the application form is ALREADY visible (no button needed)
        if self.page.query_selector("input[name='firstName']") or self.page.query_selector("[class*='ashby-application-form']"):
            if self.logger:
                self.logger.info("Form already visible, proceeding directly.", phase=ExecutionPhase.RULES)
            return True

        selectors = [
            "a:has-text('Apply')",
            "button:has-text('Apply Now')",
            "[class*='ashby-job-posting-apply']",
            "a[href*='/application']",
            "button[type='submit']",
        ]
        for selector in selectors:
            try:
                el = self.page.query_selector(selector)
                if el and el.is_visible():
                    el.click(timeout=3000)
                    if self.logger:
                        self.logger.info("Clicked Ashby apply button", phase=ExecutionPhase.RULES, selector=selector)
                    self.wait_for_page_load()
                    return True
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"Ashby apply selector failed '{selector}': {e}", phase=ExecutionPhase.RULES)
        return super().find_apply_button()

    def human_type_field(self, locator, value: str) -> bool:
        """Type into a field with exact human-like visual cadence:
        1. Smooth scroll element into center view
        2. Focus element (200ms pause)
        3. Apply green review highlight (#ecfdf5 bg, #10b981 border, glow shadow)
        4. Type character by character with 40-80ms delay per char
        5. Dispatch input and change events
        6. Hold green highlight state for 500ms
        7. Reset inline highlight and wait 500ms before next field.
        """
        import random
        from jobcli.utils.fill_guard import should_skip_refill

        if not value:
            return False

        if should_skip_refill(locator, value):
            return False

        try:
            # 1. Smooth scroll element into view (center block)
            locator.evaluate("el => el.scrollIntoView({ behavior: 'smooth', block: 'center' })")
            self.page.wait_for_timeout(250)

            # 2. Focus element (200ms)
            locator.focus(timeout=1500)
            self.page.wait_for_timeout(200)

            # 3. Green Review Highlight state
            locator.evaluate("""el => {
                el.style.transition = 'all 0.3s ease';
                el.style.border = '2px solid #10b981';
                el.style.boxShadow = '0 0 10px rgba(16, 185, 129, 0.4)';
                el.style.backgroundColor = '#ecfdf5';
            }""")

            # 4. Clear existing value
            locator.fill("")

            # 5. Type character by character with 40-80ms cadence
            for char in value:
                locator.type(char, delay=random.randint(40, 80))

            # 6. Dispatch events
            locator.evaluate("""el => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""")

            # 7. Hold green highlight state for 500ms
            self.page.wait_for_timeout(500)

            # 8. Reset inline highlight style gracefully
            locator.evaluate("""el => {
                el.style.border = '';
                el.style.boxShadow = '';
                el.style.backgroundColor = '';
            }""")

            # 9. Pause 500ms after field before moving to next field
            self.page.wait_for_timeout(500)
            return True
        except Exception as e:
            if self.logger:
                self.logger.warning(f"human_type_field fallback: {e}", phase=ExecutionPhase.RULES)
            try:
                locator.fill(value)
                return True
            except Exception:
                return False

    def fill_form(self, resume_path: Optional[str] = None) -> dict[str, Any]:
        if self.logger:
            self.logger.info("Filling Ashby form (Human-Like Visible Mode)", phase=ExecutionPhase.RULES)

        print("\n==================== ASHBY AUTOFILL - HUMAN LIKE VISIBLE MODE ====================")
        try:
            self.page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        print("Parsing your resume... Autofilling key application fields.")
        self.page.wait_for_timeout(1000)

        results: dict[str, Any] = {}
        personal = self.resume.personal

        # ── Section 1: Resume Upload ──────────────────────────────────────────
        pdf_path = resume_path or getattr(self.resume, "pdf_path", None)
        if pdf_path:
            try:
                file_input = self.page.query_selector("input[type='file']")
                if file_input:
                    file_input.set_input_files(pdf_path)
                    results["resume"] = True
                    print(f"✓ Uploaded resume: {pdf_path}")
                    self.page.wait_for_timeout(1500)  # Wait for upload animation to finish
            except Exception as e:
                print(f"ERROR uploading resume: {e}")

        self.page.wait_for_timeout(800)  # Section delay

        # ── Section 2: Personal Information ──────────────────────────────────
        zip_val = personal.zip_code or "94566"
        location_val = (
            f"{personal.city}, {personal.state}" if personal.city and personal.state
            else (personal.city or personal.state or personal.country or personal.address or "San Francisco, CA")
        )

        linkedin_val = personal.linkedin or (personal.website if personal.website and "linkedin.com" in personal.website.lower() else "")
        github_val = personal.github or (personal.website if personal.website and "github.com" in personal.website.lower() else "")

        ashby_fields = [
            ("first_name",  "input[name='firstName'], input[id*='firstName'], input[autocomplete='given-name']",  personal.first_name),
            ("last_name",   "input[name='lastName'], input[id*='lastName'], input[autocomplete='family-name']",    personal.last_name),
            ("email",       "input[name='email'], input[type='email'], input[id*='email']",                      personal.email),
            ("phone",       "input[name='phone'], input[name='phoneNumber'], input[type='tel']",                personal.phone),
            ("linkedin",    "input[name='linkedinUrl'], input[name*='linkedin'], input[id*='linkedin'], input[placeholder*='linkedin.com'], textarea[name*='linkedin'], textarea[id*='linkedin']", linkedin_val),
            ("github",      "input[name='githubUrl'], input[name*='github'], input[id*='github'], textarea[name*='github']",              github_val),
            ("portfolio",   "input[name='portfolioUrl'], input[name*='website'], input[name*='portfolio'], textarea[name*='website']",     personal.portfolio or personal.website),
            ("zip_code",    "input[name*='postalCode'], input[name*='postal_code'], input[name*='zipCode'], input[name*='zip'], input[id*='postalCode'], input[id*='zip'], input[autocomplete='postal-code']", zip_val),
            ("location",    "input[name*='location'], input[id*='location']", location_val),
        ]

        for key, selector, value in ashby_fields:
            if not value or key in results:
                continue

            try:
                el = self.page.query_selector(selector)
                if not el:
                    matched_selector = self.page.evaluate(r"""(k) => {
                        const labels = document.querySelectorAll('label, [class*="field-label"], [class*="FieldLabel"]');
                        for (const lbl of labels) {
                            const text = (lbl.innerText || '').toLowerCase();
                            if (text.includes(k)) {
                                const container = lbl.closest('div[class*="entry"], div[class*="field"], fieldset') || lbl.parentElement;
                                if (container) {
                                    const inp = container.querySelector('input, textarea');
                                    if (inp) {
                                        if (inp.id) return '#' + CSS.escape(inp.id);
                                        if (inp.getAttribute('name')) return 'input[name="' + CSS.escape(inp.getAttribute('name')) + '"], textarea[name="' + CSS.escape(inp.getAttribute('name')) + '"]';
                                    }
                                }
                            }
                        }
                        return null;
                    }""", key)
                    if matched_selector:
                        el = self.page.query_selector(matched_selector)
                        selector = matched_selector

                if el:
                    print(f"✓ Human Filling {key}...")
                    loc = self.page.locator(selector).first
                    success = self.human_type_field(loc, value)
                    if success:
                        results[key] = True
            except Exception as e:
                print(f"ERROR while filling {key}: {e}")
                results.setdefault(key, False)

        # Location Combobox
        if location_val:
            print(f"\nSelecting location: {location_val}")
            self.click_combobox("location", location_val)
            self.click_combobox("where are you located", location_val)
            self.page.wait_for_timeout(500)

        country = self.resume.personal.country or ""
        if country:
            print(f"\nSelecting country: {country}")
            self.click_combobox("Which country do you intend to work from", country)
            self.page.wait_for_timeout(500)

        self.page.wait_for_timeout(800)  # Section delay

        # ── Section 3: Questions & Experience ────────────────────────────────
        print("\nRunning fill_yes_no_questions()...")
        yes_no_count = self.fill_yes_no_questions()
        print(f"Yes/No answered: {yes_no_count}")
        self.page.wait_for_timeout(500)

        print("\nRunning fill_radio_questions()...")
        radio_count = self.fill_radio_questions()
        print(f"Radio questions answered: {radio_count}")
        self.page.wait_for_timeout(500)

        print("\nRunning fill_paragraph_questions()...")
        paragraph_count = self.fill_paragraph_questions()
        print(f"Paragraphs filled: {paragraph_count}")
        self.page.wait_for_timeout(500)

        # Fill any remaining unfilled textareas
        default_bg = (
            getattr(self.resume, "summary", None) or
            "Software engineer experienced in building web applications, APIs, and automated systems."
        )
        for ta in self.page.query_selector_all("textarea"):
            try:
                val = ta.input_value()
                if not val or not val.strip():
                    loc = self.page.locator("textarea").first
                    self.human_type_field(loc, default_bg)
            except Exception:
                pass

        # ── Section 4: Final Review Notification ────────────────────────────
        print("\n✓ Autofill completed! Please review the information filled in for you.")
        self.page.wait_for_timeout(2000)  # Keep visible 2 seconds before submit/review

        print("\n==================== ASHBY AUTOFILL END ====================\n")

        if self.logger:
            self.logger.info(
                "Ashby form fill complete",
                phase=ExecutionPhase.RULES,
                results=results,
            )

        return results

    def is_success(self) -> bool:
        try:
            if self.page.locator("text='Your application was successfully submitted'").is_visible(timeout=2000):
                return True
            if self.page.locator("text='Application Submitted'").is_visible(timeout=2000):
                return True
            if self.page.locator("text='Thank you for applying'").is_visible(timeout=2000):
                return True
        except Exception:
            pass
        return False

    def has_validation_errors(self) -> bool:
        """Check if red error banner or required field errors exist on screen."""
        try:
            if self.page.locator("text='Your form needs corrections'").is_visible(timeout=1000):
                return True
            if self.page.locator("text='Missing entry for required field'").is_visible(timeout=1000):
                return True
            if self.page.locator("[class*='error-message'], [class*='errorMessage'], [class*='field-error']").is_visible(timeout=1000):
                return True
        except Exception:
            pass
        return False

    def auto_fix_validation_errors(self) -> None:
        """Auto-fill missing Zip Code, Location, or required fields when validation fails."""
        personal = self.resume.personal
        zip_val = personal.zip_code or "94566"
        location_val = (
            f"{personal.city}, {personal.state}" if personal.city and personal.state
            else (personal.city or personal.state or personal.country or personal.address or "San Francisco, CA")
        )

        print("[INFO] Running auto-fix for missing required fields...")
        # Zip Code auto-fill
        zip_inputs = self.page.query_selector_all("input[name*='postal'], input[name*='zip'], input[id*='postal'], input[id*='zip']")
        for inp in zip_inputs:
            try:
                inp.fill(zip_val)
                print(f"✓ Auto-filled zip/postal code: {zip_val}")
            except Exception:
                pass

        # Location auto-fill
        self.click_combobox("location", location_val)
        loc_inputs = self.page.query_selector_all("input[name*='location'], input[id*='location']")
        for inp in loc_inputs:
            try:
                inp.fill(location_val)
                inp.press("ArrowDown")
                inp.press("Enter")
                print(f"✓ Auto-filled location: {location_val}")
            except Exception:
                pass

    def submit_application(self) -> bool:
        for selector in [
            "button[type='submit']",
            "button:has-text('Submit Application')",
            "button:has-text('Submit')",
            "button[data-test*='submit']",
            "form button:not([type='button'])",
        ]:
            try:
                el = self.page.query_selector(selector)
                if el and el.is_visible():
                    print(f"✓ Clicking submit button: {selector}")
                    el.click(timeout=5000)
                    self.page.wait_for_timeout(2000)

                    # Check if Ashby displayed red validation errors
                    if self.has_validation_errors():
                        print("[WARNING] Ashby form displayed validation errors after submit! Running auto-fix...")
                        self.auto_fix_validation_errors()
                        # Re-click submit
                        el.click(timeout=5000)
                        self.page.wait_for_timeout(2000)

                    if not self.has_validation_errors() or self.is_success():
                        print("✓ Form submitted cleanly (0 validation errors on screen)")
                        return True
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"Ashby submit failed '{selector}': {e}", phase=ExecutionPhase.RULES)
        return not self.has_validation_errors()

    def handle_multi_step(self, state: ApplicationState) -> bool:
        return super().handle_multi_step(state)

    # ------------------------------------------------------------------
    # Rule-based Yes/No auto-selection (no LLM)
    # ------------------------------------------------------------------
    def fill_yes_no_questions(self) -> int:
        """Scan the page for Yes/No button-pair questions and select the correct answer.

        Uses deterministic keyword rules against resume data — no LLM involved.
        Returns the number of questions successfully answered.
        """
        answered = 0
        try:
            # Collect all button-pair question containers via JS.
            # We look for Ashby's styled <button> Yes/No pairs inside question wrappers.
            question_pairs = self.page.evaluate(r"""() => {
                const results = [];

                // Ashby question containers — try known class patterns and generic rows
                const containerSelectors = [
                    '.ashby-application-form-field-entry',
                    '.ashby-application-form-question',
                    '[class*="ashby-application-form"]',
                    '[class*="form-field"]',
                    '[class*="field-entry"]',
                ];

                // Title element patterns within a container
                const titleSelectors = [
                    'legend', 'label', '[class*="question-title"]',
                    '[class*="field-label"]', '[class*="FieldLabel"]',
                    'strong', 'h3', 'h4', 'p',
                ];

                const seen = new Set();

                // Also scan all fieldsets (standard radios)
                for (const fs of document.querySelectorAll(
                    'fieldset, [role="radiogroup"], [role="group"]'
                )) {
                    const legend = fs.querySelector(':scope > legend, :scope > label');
                    const text = (legend ? legend.innerText : fs.getAttribute('aria-label') || '').trim();
                    if (!text || seen.has(fs)) continue;

                    const btns = [...fs.querySelectorAll(
                        'input[type="radio"], input[type="checkbox"], ' +
                        'button, [role="button"], [role="radio"]'
                    )];
                    if (btns.length < 2) continue;

                    const optionTexts = btns.map(b => {
                        const label = b.closest('label') ||
                            (b.id && document.querySelector('label[for="' + CSS.escape(b.id) + '"]'));
                        return (label ? label.innerText : b.innerText || b.getAttribute('value') || '').trim();
                    }).filter(Boolean);

                    const isYesNo = optionTexts.some(t => /^yes$/i.test(t)) &&
                                    optionTexts.some(t => /^no$/i.test(t));
                    if (!isYesNo) continue;

                    seen.add(fs);
                    results.push({ question: text, options: optionTexts });
                }

                // Scan title elements for button-pair Yes/No
                const titleCandidates = document.querySelectorAll(
                    'label, legend, [class*="question-title"], [class*="field-label"], ' +
                    '[class*="FieldLabel"], [class*="field_label"], strong, h3, h4, p'
                );
                for (const titleEl of titleCandidates) {
                    const text = (titleEl.innerText || '').trim();
                    if (!text || text.length < 5 || seen.has(titleEl)) continue;

                    // Walk up to find a container with buttons
                    let container = titleEl.parentElement;
                    for (let i = 0; i < 7 && container; i++) {
                        const btns = [...container.querySelectorAll(
                            'button, [role="button"], [role="radio"], [role="switch"]'
                        )].filter(b => b !== titleEl && b.innerText && b.innerText.trim().length > 0);

                        if (btns.length >= 2) {
                            const optionTexts = btns.map(b => (b.innerText || '').trim()).filter(Boolean);
                            const isYesNo = optionTexts.some(t => /^yes$/i.test(t)) &&
                                            optionTexts.some(t => /^no$/i.test(t));
                            if (isYesNo && !seen.has(container)) {
                                seen.add(container);
                                seen.add(titleEl);
                                results.push({ question: text, options: optionTexts });
                            }
                            break;
                        }
                        container = container.parentElement;
                    }
                }

                return results;
            }""")

            if not question_pairs:
                return 0

            if self.logger:
                self.logger.info(
                    f"Ashby: found {len(question_pairs)} Yes/No question(s)",
                    phase=ExecutionPhase.RULES,
                )

            for pair in question_pairs:
                question_text = pair.get("question", "")
                answer = self._resolve_yes_no_answer(question_text)
                if not answer:
                    continue

                success = self.click_option(question_text, answer)
                if success:
                    answered += 1
                    if self.logger:
                        self.logger.info(
                            f"Ashby Yes/No: '{question_text[:60]}' → '{answer}'",
                            phase=ExecutionPhase.RULES,
                        )
                else:
                    if self.logger:
                        self.logger.warning(
                            f"Ashby Yes/No: failed to click '{answer}' for '{question_text[:60]}'",
                            phase=ExecutionPhase.RULES,
                        )

        except Exception as e:
            if self.logger:
                self.logger.warning(f"fill_yes_no_questions error: {e}", phase=ExecutionPhase.RULES)

        return answered

    def fill_radio_questions(self) -> int:
        """Find all radio groups and multi-choice questions on Ashby forms and select the optimal answer.

        Automatically selects top experience tiers ('Quite a bit', 'I'm an expert', '3-5 years', '5+ years')
        so no radio group is left unanswered.
        """
        answered = 0
        try:
            radio_groups = self.page.evaluate(r"""() => {
                const results = [];
                const groups = document.querySelectorAll('fieldset, [role="radiogroup"], [role="group"]');

                for (const grp of groups) {
                    const rect = grp.getBoundingClientRect();
                    if (rect.width < 2 || rect.height < 2) continue;

                    const legend = grp.querySelector('legend, label, [class*="question-title"], [class*="field-label"]');
                    const text = (legend ? legend.innerText : grp.getAttribute('aria-label') || '').trim();
                    if (!text) continue;

                    const radios = [...grp.querySelectorAll('input[type="radio"], [role="radio"]')];
                    if (radios.length < 2) continue;

                    const isChecked = radios.some(r => r.checked || r.getAttribute('aria-checked') === 'true');
                    if (isChecked) continue;

                    const options = radios.map(r => {
                        const lbl = r.closest('label') || (r.id && document.querySelector('label[for="' + CSS.escape(r.id) + '"]'));
                        const valText = (lbl ? lbl.innerText : r.getAttribute('value') || r.innerText || '').trim();
                        return valText;
                    }).filter(Boolean);

                    if (options.length >= 2) {
                        results.push({ question: text, options });
                    }
                }
                return results;
            }""")

            EEO_PATTERNS = re.compile(
                r"gender|race|ethnic|veteran|disability|eeo|demographic|self-identif|voluntary|protected|sexual\s*orientation|\bage\b|current\s*age",
                re.IGNORECASE,
            )

            for item in radio_groups:
                question = item.get("question", "")
                options = item.get("options", [])
                if not options:
                    continue

                if re.search(r"transgender", question, re.IGNORECASE):
                    print(f"✓ Transgender question detected → selecting 'No'")
                    success = self.click_option(question, "No")
                    if success:
                        answered += 1
                    continue

                if EEO_PATTERNS.search(question):
                    print(f"⏩ Skipping EEO/demographic radio question for manual review: '{question[:60]}'")
                    continue

                best_choice = self._resolve_radio_choice(question, options)
                if best_choice:
                    success = self.click_option(question, best_choice)
                    if success:
                        answered += 1
                        print(f"✓ Ashby Radio: '{question[:60]}' → '{best_choice}'")

        except Exception as e:
            if self.logger:
                self.logger.warning(f"fill_radio_questions error: {e}", phase=ExecutionPhase.RULES)

        return answered

    def _resolve_radio_choice(self, question: str, option_texts: list[str]) -> str:
        """Select the optimal choice for a multi-choice radio question based on experience tiers."""
        cleaned_opts = [t.strip() for t in option_texts if t and t.strip()]
        if not cleaned_opts:
            return ""

        # Priority 1: Expert / Advanced / High experience keywords
        expert_patterns = [
            r"expert", r"quite a bit", r"5\+\s*year", r"5\s*-\s*7", r"3\s*-\s*5",
            r"advanced", r"senior", r"proficient", r"extensive", r"strong",
            r"master", r"leader", r"heavy", r"production"
        ]
        for pat in expert_patterns:
            for opt in cleaned_opts:
                if re.search(pat, opt, re.IGNORECASE):
                    return opt

        # Priority 2: Exclude 'Never' / 'None' / 'A little' / '0'
        negative_pat = re.compile(r"never|none|0\s*year|no\s*experience|a little|basic", re.IGNORECASE)
        positive_opts = [o for o in cleaned_opts if not negative_pat.search(o)]
        if positive_opts:
            return positive_opts[-1]

        return cleaned_opts[-1] if len(cleaned_opts) > 1 else cleaned_opts[0]

    def _resolve_yes_no_answer(self, question_text: str) -> Optional[str]:
        """Determine Yes or No for a question using rule-based resume data lookup.

        Returns 'Yes', 'No', or None (skip / can't determine).
        """
        q = question_text.strip()
        if not q:
            return None

        # Skip EEO / demographic questions so they are left for manual user review
        if re.search(r"gender|race|ethnic|veteran|disability|eeo|demographic|self-identif|voluntary|protected|\bage\b|current\s*age", q, re.IGNORECASE):
            if re.search(r"transgender", q, re.IGNORECASE):
                return "No"
            return None

        wa = self.resume.work_authorization

        for pattern, answer_if_true, answer_if_false, attr_path in _YES_NO_RULES:
            if not pattern.search(q):
                continue

            if attr_path == "_hardcoded_yes":
                return answer_if_true

            if attr_path == "_hardcoded_no":
                return answer_if_false

            if attr_path == "_currently_employed":
                # Check if most recent experience has no end_date / is current
                exp_list = self.resume.experience or []
                if exp_list:
                    latest = exp_list[0]
                    is_current = latest.current or not latest.end_date
                    return answer_if_true if is_current else answer_if_false
                return answer_if_false

            # Dotted attribute path into ResumeData
            try:
                if attr_path is None:
                    return answer_if_false
                parts = attr_path.split(".")
                obj: Any = self.resume
                for part in parts:
                    obj = getattr(obj, part, None)
                    if obj is None:
                        break
                # obj is now the bool value (or None)
                if obj is None:
                    return answer_if_false   # safe default
                return answer_if_true if bool(obj) else answer_if_false
            except Exception:
                return answer_if_false

        # No rule matched — default to "No" for sponsorship/clearance, "Yes" for work auth & generic questions
        if re.search(r"sponsor|visa|clearance|felon|crime", q, re.IGNORECASE):
            return "No"
        return "Yes"

    # ------------------------------------------------------------------
    # Rule-based paragraph/textarea auto-fill (no LLM)
    # ------------------------------------------------------------------
    def fill_paragraph_questions(self) -> int:
        """Find all visible textarea fields and fill them with resume-derived content.

        Uses keyword matching on the question label — no LLM involved.
        Returns the number of fields successfully filled.
        """
        filled_count = 0
        try:
            # Gather all visible textarea elements with their associated question text
            textarea_info = self.page.evaluate(r"""() => {
                const results = [];
                const textareas = document.querySelectorAll('textarea:not([disabled])');

                for (const ta of textareas) {
                    const rect = ta.getBoundingClientRect();
                    if (rect.width < 2 || rect.height < 2) continue;
                    if (ta.getAttribute('aria-hidden') === 'true') continue;

                    // Skip already-filled textareas
                    const current = (ta.value || '').trim();
                    if (current.length > 10) continue;

                    // Resolve question label
                    let questionText = '';

                    // 1) aria-labelledby
                    const lby = (ta.getAttribute('aria-labelledby') || '').trim();
                    if (lby) {
                        questionText = lby.split(/\s+/)
                            .map(id => { const e = document.getElementById(id); return e ? e.innerText : ''; })
                            .filter(Boolean).join(' ');
                    }

                    // 2) aria-label
                    if (!questionText) questionText = ta.getAttribute('aria-label') || '';

                    // 3) associated <label>
                    if (!questionText && ta.id) {
                        const lbl = document.querySelector('label[for="' + CSS.escape(ta.id) + '"]');
                        if (lbl) questionText = lbl.innerText || '';
                    }

                    // 4) parent label
                    if (!questionText) {
                        const pl = ta.closest('label');
                        if (pl) questionText = pl.innerText || '';
                    }

                    // 5) Walk up DOM for nearby question text (Ashby pattern)
                    if (!questionText) {
                        let p = ta.parentElement;
                        for (let i = 0; i < 6 && p; i++) {
                            // Look for a legend, label, or question-title sibling/ancestor
                            const candidate = p.querySelector(
                                'legend, label, [class*="question-title"], ' +
                                '[class*="field-label"], [class*="FieldLabel"], p, strong'
                            );
                            if (candidate && candidate !== ta) {
                                const t = (candidate.innerText || '').trim();
                                if (t.length > 5) { questionText = t; break; }
                            }
                            p = p.parentElement;
                        }
                    }

                    // 6) placeholder as last resort
                    if (!questionText) questionText = ta.getAttribute('placeholder') || '';

                    // Build a CSS selector for this textarea
                    let selector = '';
                    if (ta.id)   selector = '#' + ta.id;
                    else if (ta.name) selector = 'textarea[name="' + ta.name + '"]';
                    else {
                        // positional fallback — find index among textareas
                        const all = [...document.querySelectorAll('textarea')];
                        const idx = all.indexOf(ta);
                        if (idx >= 0) selector = 'textarea:nth-of-type(' + (idx + 1) + ')';
                    }

                    if (selector) {
                        results.push({ selector, question: questionText.trim() });
                    }
                }
                return results;
            }""")

            if not textarea_info:
                return 0

            if self.logger:
                self.logger.info(
                    f"Ashby: found {len(textarea_info)} unfilled textarea(s)",
                    phase=ExecutionPhase.RULES,
                )

            used_answers: set[str] = set()

            for item in textarea_info:
                selector = item.get("selector", "")
                question = item.get("question", "")
                if not selector:
                    continue

                answer = self._build_paragraph_answer(question, used_answers=used_answers)
                if not answer:
                    continue

                used_answers.add(answer)

                try:
                    loc = self.page.locator(selector).first
                    self.humanized_fill(loc, answer)
                    filled_count += 1
                    if self.logger:
                        self.logger.info(
                            f"Ashby paragraph fill: '{question[:60]}' → {len(answer)} chars",
                            phase=ExecutionPhase.RULES,
                            selector=selector,
                        )
                except Exception as e:
                    if self.logger:
                        self.logger.warning(
                            f"Ashby paragraph fill failed '{selector}': {e}",
                            phase=ExecutionPhase.RULES,
                        )

        except Exception as e:
            if self.logger:
                self.logger.warning(f"fill_paragraph_questions error: {e}", phase=ExecutionPhase.RULES)

        return filled_count

    def _default_experience_answer(self, used_answers: Optional[set[str]] = None) -> str:
        """Return a unique experience description or profile summary as fallback."""
        used = used_answers if used_answers is not None else set()

        for exp in (self.resume.experience or []):
            if exp.description and exp.description.strip() not in used:
                return exp.description.strip()

        candidates = [
            "Experienced software engineer skilled in full-stack development, API design, and system architecture.",
            "Demonstrated track record of delivering scalable web applications, backend microservices, and automated data pipelines.",
            "Strong technical proficiency in designing robust software solutions and collaborating across cross-functional teams.",
            "Passionate software developer dedicated to building high-performance, reliable, and user-centric applications.",
        ]
        for cand in candidates:
            if cand not in used:
                return cand
        return candidates[0]

    def _build_paragraph_answer(self, question_text: str, used_answers: Optional[set[str]] = None) -> Optional[str]:
        """Build a resume-derived answer for a paragraph/textarea question."""
        q = question_text.strip()
        q_lower = q.lower()
        used = used_answers if used_answers is not None else set()

        # Handle URL / link / contact fields that render as textareas
        if "linkedin" in q_lower:
            return self.resume.personal.linkedin
        if "github" in q_lower:
            return self.resume.personal.github
        if "portfolio" in q_lower or "website" in q_lower:
            return self.resume.personal.portfolio or self.resume.personal.website
        if "twitter" in q_lower:
            return getattr(self.resume.personal, "twitter", None) or ""
        if "phone" in q_lower:
            return self.resume.personal.phone
        if "email" in q_lower:
            return self.resume.personal.email

        # Helper: gather experience descriptions (most recent first)
        def _experience_descriptions() -> list[str]:
            descs = []
            for exp in (self.resume.experience or []):
                if exp.description:
                    descs.append(exp.description.strip())
            return descs

        def _latest_job_title() -> str:
            exp_list = self.resume.experience or []
            if exp_list and exp_list[0].title:
                return exp_list[0].title
            return ""

        def _latest_company() -> str:
            exp_list = self.resume.experience or []
            if exp_list and exp_list[0].company:
                return exp_list[0].company
            return ""

        def _skills_sentence() -> str:
            skills = self.resume.skills or []
            if not skills:
                return ""
            top = skills[:6]
            if len(top) > 1:
                return "My key skills include " + ", ".join(top[:-1]) + " and " + top[-1] + "."
            return "My key skill is " + top[0] + "."

        def _personal_name() -> str:
            p = self.resume.personal
            parts = [p.first_name or "", p.last_name or ""]
            return " ".join(x for x in parts if x).strip()

        descs = _experience_descriptions()
        first_desc = descs[0] if descs else ""

        # Match question against rules table
        for pattern, answer_type in _PARAGRAPH_RULES:
            if not pattern.search(q):
                continue

            if answer_type == "outcome_experience":
                result_kw = re.compile(r"achiev|result|deliver|impact|improv|reduc|increas|launch|build|led|drove|success|metric|worked", re.IGNORECASE)
                achievement_descs = [d for d in descs if result_kw.search(d)]
                for d in achievement_descs + descs:
                    if d not in used:
                        return d
                return self._default_experience_answer(used)

            if answer_type == "project_experience":
                project_kw = re.compile(r"build|develop|create|design|project|agent|ai|model|platform|system|tool", re.IGNORECASE)
                project_descs = [d for d in descs if project_kw.search(d)]
                for d in project_descs + descs:
                    if d not in used:
                        return d
                return self._default_experience_answer(used)

            if answer_type == "technical_experience":
                for d in descs:
                    if d not in used:
                        return d
                return self._default_experience_answer(used)

            if answer_type == "product_experience":
                parts = []
                if _skills_sentence():
                    parts.append(_skills_sentence())
                if first_desc:
                    parts.append(first_desc)
                ans = " ".join(parts)
                if ans not in used:
                    return ans
                return self._default_experience_answer(used)

            if answer_type == "decision_experience":
                for d in reversed(descs):
                    if d not in used:
                        return d
                return self._default_experience_answer(used)

            if answer_type == "collaboration_experience":
                collab_kw = re.compile(r"team|collab|cross|partner|stakeholder|work\s+with", re.IGNORECASE)
                collab = [d for d in descs if collab_kw.search(d)]
                for d in collab + descs:
                    if d not in used:
                        return d
                return self._default_experience_answer(used)

            if answer_type == "motivation":
                title = _latest_job_title()
                skills = _skills_sentence()
                company = _latest_company()
                parts = []
                if title:
                    parts.append(f"I am a {title} with a strong passion for building impactful solutions.")
                if skills:
                    parts.append(skills)
                if company:
                    parts.append(f"My experience at {company} has given me a solid foundation to excel in this role.")
                ans = " ".join(parts)
                if ans not in used:
                    return ans
                return self._default_experience_answer(used)

            if answer_type == "background":
                name = _personal_name()
                title = _latest_job_title()
                skills = _skills_sentence()
                parts = []
                if name and title:
                    parts.append(f"I am {name}, a {title}.")
                elif title:
                    parts.append(f"I am a {title}.")
                if skills:
                    parts.append(skills)
                if first_desc:
                    parts.append(first_desc)
                ans = " ".join(parts)
                if ans not in used:
                    return ans
                return self._default_experience_answer(used)

            if answer_type == "skills_experience":
                parts = []
                if _skills_sentence():
                    parts.append(_skills_sentence())
                if first_desc:
                    parts.append(first_desc)
                ans = " ".join(parts)
                if ans not in used:
                    return ans
                return self._default_experience_answer(used)

            if answer_type == "achievement":
                result_kw = re.compile(r"achiev|result|deliver|impact|improv|reduc|increas|launch|build|led|drove", re.IGNORECASE)
                achievement_descs = [d for d in descs if result_kw.search(d)]
                for d in achievement_descs + descs:
                    if d not in used:
                        return d
                return self._default_experience_answer(used)

        # No rule matched — use generic non-repeating fallback
        return self._default_experience_answer(used)

    # ------------------------------------------------------------------
    # Ashby radio / checkbox / button-segment click
    # ------------------------------------------------------------------
    def click_option(self, question: str, value: str) -> Optional[bool]:
        """Ashby radio / checkbox / button-segment click.

        Ashby uses two distinct DOM patterns for single-choice questions:

        1. **Standard radios**: ``<fieldset><legend>Q?</legend>
           <label><input type="radio" value="Yes"/>Yes</label>...</fieldset>``
           Common for compliance, EEO, demographics.

        2. **Button-segmented control**: two (or more) ``<button>`` elements
           side-by-side inside a question container, e.g. for the
           "Are you eligible for a U.S Security Clearance?" Yes/No pair.
           These have no ``<input>`` backing - just styled buttons with
           visible text "Yes" / "No" and ``aria-pressed`` / ``aria-checked``
           to reflect state.

        We try #1 first (most common), then #2. Returns True on success,
        False if Ashby-specific DOM is detected but no option matched,
        None if nothing Ashby-like was found (so the caller falls through
        to the generic pipeline).
        """
        value = (value or "").strip()
        if self.logger:
            self.logger.info(
                f"click_option called: question='{question}', value='{value}'",
                phase=ExecutionPhase.RULES 
            )
        if not question or not value:
            return None
        try:
            js = r"""(args) => {
                const q = (args.q || '').toLowerCase().replace(/\s+/g, ' ').trim();
                const v = (args.v || '').toLowerCase().replace(/\s+/g, ' ').trim();
                if (!q || !v) return {ok: false, reason: 'empty'};
                const qTokens = q.split(/\s+/).filter(t => t.length >= 3);
                const matchesQuestion = (text) => {
                    const n = (text || '').toLowerCase().replace(/\s+/g, ' ').trim();
                    if (!n) return 0;
                    if (n === q) return 3;
                    if (n.includes(q)) return 2;
                    return qTokens.length && qTokens.every(t => n.includes(t)) ? 1 : 0;
                };
                const matchesValue = (el) => {
                    const attrVal = (el.getAttribute('value') || '').toLowerCase().trim();
                    if (attrVal === v) return 3;
                    const labels = [];
                    if (el.id) {
                        document.querySelectorAll('label[for="' + CSS.escape(el.id) + '"]')
                            .forEach(l => labels.push(l.textContent));
                    }
                    const wrap = el.closest('label');
                    if (wrap) labels.push(wrap.textContent);
                    const aria = el.getAttribute('aria-label');
                    if (aria) labels.push(aria);
                    for (const t of labels) {
                        const n = (t || '').toLowerCase().trim();
                        if (n === v) return 2;
                        if (n.includes(v) && v.length >= 2) return 1;
                    }
                    return attrVal && attrVal.includes(v) ? 1 : 0;
                };
                // ── Pass 1: native radio / checkbox inside a fieldset ──
                const fieldsets = [...document.querySelectorAll(
                    'fieldset, [role="radiogroup"], [role="group"]'
                )];
                let best = null;
                let bestScore = 0;
                for (const fs of fieldsets) {
                    let qScore = 0;
                    const legend = fs.querySelector(
                        ':scope > legend, :scope > label, :scope > .ashby-application-form-question-title'
                    );
                    if (legend) qScore = Math.max(qScore, matchesQuestion(legend.textContent));
                    const aria = fs.getAttribute('aria-label');
                    if (aria) qScore = Math.max(qScore, matchesQuestion(aria));
                    const lby = fs.getAttribute('aria-labelledby');
                    if (lby) {
                        const t = lby.split(/\s+/).map(id =>
                            document.getElementById(id)?.textContent || ''
                        ).join(' ');
                        qScore = Math.max(qScore, matchesQuestion(t));
                    }
                    if (qScore === 0) continue;
                    const inputs = fs.querySelectorAll('input[type="radio"], input[type="checkbox"]');
                    for (const inp of inputs) {
                        const vScore = matchesValue(inp);
                        if (vScore === 0) continue;
                        const total = qScore * 10 + vScore;
                        if (total > bestScore) { best = inp; bestScore = total; }
                    }
                }
                if (best) {
                    try { best.scrollIntoView({block: 'center'}); } catch (e) {}
                    const isOn = () => best.checked === true ||
                        best.getAttribute('aria-checked') === 'true';
                    if (!isOn()) { try { best.click(); } catch (e) {} }
                    if (!isOn()) {
                        const lab = best.closest('label') ||
                            (best.id && document.querySelector('label[for="' + CSS.escape(best.id) + '"]'));
                        if (lab) { try { lab.click(); } catch (e) {} }
                    }
                    if (!isOn()) {
                        const setter = Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype, 'checked'
                        )?.set;
                        if (setter) setter.call(best, true); else best.checked = true;
                        best.dispatchEvent(new Event('input', {bubbles: true}));
                        best.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                    return {ok: isOn(), score: bestScore, mode: 'radio'};
                }

                // ── Pass 2: button-segmented control (e.g. Yes / No buttons) ──
                // Ashby renders some Yes/No questions as two styled <button>s
                // rather than radios. Locate the question container by its
                // visible title/label, then find the child button whose
                // accessible text matches the value.
                const matchesButtonText = (txt) => {
                    const n = (txt || '').toLowerCase().replace(/\s+/g, ' ').trim();
                    if (!n) return 0;
                    if (n === v) return 3;
                    if (n === v.replace(/[^a-z0-9]/g, '')) return 3;
                    if (n.includes(v)) return 2;
                    return 0;
                };
                // Find candidate title nodes whose text matches the question.
                const titleCandidates = [...document.querySelectorAll(
                    'label, legend, [class*="question-title"], [class*="field-label"], ' +
                    '[class*="FieldLabel"], [class*="field_label"], strong, h3, h4'
                )];
                let buttonBest = null;
                let buttonBestScore = 0;
                for (const titleEl of titleCandidates) {
                    const qScore = matchesQuestion(titleEl.textContent);
                    if (qScore === 0) continue;
                    // Walk up to find the nearest container that also
                    // contains clickable buttons.
                    let container = titleEl;
                    for (let i = 0; i < 6 && container; i++) {
                        const btns = container.querySelectorAll(
                            'button, [role="button"], [role="radio"], [role="switch"]'
                        );
                        if (btns.length > 0) {
                            for (const b of btns) {
                                if (b === titleEl) continue;
                                const txt = (b.innerText || b.textContent || '').trim();
                                const aria = b.getAttribute('aria-label') || '';
                                const vScore = Math.max(
                                    matchesButtonText(txt),
                                    matchesButtonText(aria),
                                );
                                if (vScore === 0) continue;
                                const total = qScore * 10 + vScore;
                                if (total > buttonBestScore) {
                                    buttonBest = b;
                                    buttonBestScore = total;
                                }
                            }
                            if (buttonBest) break;
                        }
                        container = container.parentElement;
                    }
                    if (buttonBest) break;
                }
                if (buttonBest) {
                    try { buttonBest.scrollIntoView({block: 'center'}); } catch (e) {}
                    const wasPressed = () => (
                        buttonBest.getAttribute('aria-pressed') === 'true' ||
                        buttonBest.getAttribute('aria-checked') === 'true' ||
                        buttonBest.getAttribute('data-state') === 'on' ||
                        buttonBest.classList.contains('selected') ||
                        buttonBest.classList.contains('active') ||
                        buttonBest.classList.contains('is-selected')
                    );
                    try { buttonBest.click(); } catch (e) {}
                    // Some Ashby buttons swallow plain .click() and only
                    // react to PointerEvent sequences — try that as a
                    // second attempt before giving up.
                    if (!wasPressed()) {
                        try {
                            const fire = (type) => buttonBest.dispatchEvent(
                                new PointerEvent(type, {bubbles: true, cancelable: true})
                            );
                            fire('pointerdown');
                            fire('pointerup');
                            fire('click');
                        } catch (e) {}
                    }
                    return {
                        ok: true, // we issued a click on the right button
                        score: buttonBestScore,
                        mode: 'button',
                        confirmed: wasPressed(),
                    };
                }
                return {ok: false, reason: 'no-match'};
            }"""
            for target in [self.page] + list(self.page.frames):
                try:
                    res = target.evaluate(js, {"q": question, "v": value})
                    if res and res.get("ok"):
                        self.page.wait_for_timeout(250)  # Visual pause after option selection
                        if self.logger:
                            self.logger.info(
                                f"Ashby option click OK: '{question}' = '{value}' "
                                f"(mode={res.get('mode')}, "
                                f"score={res.get('score')}, "
                                f"confirmed={res.get('confirmed', True)})",
                                phase=ExecutionPhase.RULES,
                            )
                        return True
                except Exception:
                    continue
            return False
        except Exception as e:
            if self.logger:
                self.logger.warning(
                    f"Ashby click_option error: {e}", phase=ExecutionPhase.RULES
                )
            return False

    # ------------------------------------------------------------------
    # Ashby combobox / dropdown handler (fixed JS)
    # ------------------------------------------------------------------
    def click_combobox(self, question: str, value: str) -> bool:
        """Handle Ashby combobox/dropdown fields.

        Fixed from original: corrected indentation (now a class method),
        fixed JS setTimeout typo, and fixed unclosed trim() parenthesis.
        """
        if not question or not value:
            return False

        try:
            js = r"""
            (args) => {
                const question = (args.question || '').toLowerCase().trim();
                const value = (args.value || '').toLowerCase().trim();
                if (!question || !value) return false;

                // Find labels / legends that match the question text
                const labels = [
                    ...document.querySelectorAll('label'),
                    ...document.querySelectorAll('legend'),
                    ...document.querySelectorAll('[class*=field-label]'),
                    ...document.querySelectorAll('[class*=question-title]'),
                ];

                for (const label of labels) {
                    const txt = (label.innerText || '').toLowerCase();
                    if (!txt.includes(question)) continue;

                    let container = label.parentElement;
                    while (container) {
                        const combo = container.querySelector(
                            '[role="combobox"], button[aria-haspopup="listbox"], ' +
                            'button[aria-haspopup="true"], select'
                        );
                        if (combo) {
                            // Handle native <select>
                            if (combo.tagName === 'SELECT') {
                                const options = [...combo.options];
                                const match = options.find(o =>
                                    o.text.toLowerCase().trim() === value ||
                                    o.value.toLowerCase().trim() === value ||
                                    o.text.toLowerCase().includes(value)
                                );
                                if (match) {
                                    combo.value = match.value;
                                    combo.dispatchEvent(new Event('change', {bubbles: true}));
                                    return true;
                                }
                            } else {
                                // Custom combobox — click to open, then pick option
                                combo.click();
                                return new Promise(resolve => {
                                    setTimeout(() => {
                                        const options = [...document.querySelectorAll('[role="option"]')];
                                        const match = options.find(o =>
                                            (o.innerText || '').toLowerCase().trim() === value
                                        );
                                        if (match) {
                                            match.click();
                                            resolve(true);
                                        } else {
                                            // Try partial match
                                            const partial = options.find(o =>
                                                (o.innerText || '').toLowerCase().includes(value)
                                            );
                                            if (partial) {
                                                partial.click();
                                                resolve(true);
                                            } else {
                                                resolve(false);
                                            }
                                        }
                                    }, 350);
                                });
                            }
                        }
                        container = container.parentElement;
                    }
                }
                return false;
            }
            """

            result = self.page.evaluate(js, {
                "question": question,
                "value": value,
            })
            # evaluate() resolves Promises automatically in Playwright
            return bool(result)

        except Exception as e:
            if self.logger:
                self.logger.warning(
                    f"Ashby click_combobox error for '{question}': {e}",
                    phase=ExecutionPhase.RULES,
                )
            return False