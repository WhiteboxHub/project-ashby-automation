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
]

# Maps paragraph-question keyword patterns → how to build the answer from resume
_PARAGRAPH_RULES: list[tuple[re.Pattern, str]] = [
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

    def fill_form(self, resume_path: Optional[str] = None) -> dict[str, Any]:
        if self.logger:
            self.logger.info("Filling Ashby form", phase=ExecutionPhase.RULES)

        print("\n==================== Ashby fill_form START ====================")

        results: dict[str, Any] = {}
        personal = self.resume.personal

        ashby_fields = [
            ("first_name",  "input[name='firstName']",    personal.first_name),
            ("last_name",   "input[name='lastName']",     personal.last_name),
            ("email",       "input[name='email']",        personal.email),
            ("phone",       "input[name='phone']",        personal.phone),
            ("phone",       "input[name='phoneNumber']",  personal.phone),
            ("linkedin",    "input[name='linkedinUrl']",  personal.linkedin),
            ("github",      "input[name='githubUrl']",    personal.github),
        ]

        for key, selector, value in ashby_fields:
            if not value or key in results:
                continue

            try:
                print(f"\nChecking field: {key}")
                print(f"Selector: {selector}")

                el = self.page.query_selector(selector)

                if el:
                    print(f"✓ Found {key}")
                    self.humanized_fill(self.page.locator(selector).first, value)
                    results[key] = True
                else:
                    print(f"✗ {key} not found")

            except Exception as e:
                print(f"ERROR while filling {key}: {e}")

                if self.logger:
                    self.logger.warning(
                        f"Ashby fill failed '{key}': {e}",
                        phase=ExecutionPhase.RULES,
                    )

                results.setdefault(key, False)

        print("\nRunning generic_fill_failed_fields()...")
        results = self.generic_fill_failed_fields(results)

        print("\nRunning fill_yes_no_questions()...")
        yes_no_count = self.fill_yes_no_questions()
        print(f"Yes/No answered: {yes_no_count}")

        print("\nRunning fill_paragraph_questions()...")
        paragraph_count = self.fill_paragraph_questions()
        print(f"Paragraphs filled: {paragraph_count}")

        country = self.resume.personal.country or ""

        print(f"\nSelecting country: {country}")

        country_result = self.click_combobox(
            "Which country do you intend to work from",
            country,
        )

        print(f"Country selection result: {country_result}")

        print("\n==================== Ashby fill_form END ====================\n")

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
        except Exception:
            pass
        return super().is_success()

    def submit_application(self) -> bool:
        for selector in ["button[type='submit']", "button:has-text('Submit Application')", "button:has-text('Submit')"]:
            try:
                el = self.page.query_selector(selector)
                if el and el.is_visible():
                    el.click(timeout=3000)
                    self.wait_for_page_load()
                    return True
            except Exception as e:
                if self.logger:
                    self.logger.warning(f"Ashby submit failed '{selector}': {e}", phase=ExecutionPhase.RULES)
        return super().submit_application()

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

    def _resolve_yes_no_answer(self, question_text: str) -> Optional[str]:
        """Determine Yes or No for a question using rule-based resume data lookup.

        Returns 'Yes', 'No', or None (skip / can't determine).
        """
        q = question_text.strip()
        if not q:
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

        # No rule matched — default to "No" (safest generic default)
        return "No"

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

            for item in textarea_info:
                selector = item.get("selector", "")
                question = item.get("question", "")
                if not selector:
                    continue

                answer = self._build_paragraph_answer(question)
                if not answer:
                    continue

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

    def _build_paragraph_answer(self, question_text: str) -> Optional[str]:
        """Build a resume-derived answer for a paragraph/textarea question.

        Selects the most relevant experience description or skill summary
        based on keyword matching of the question text. No LLM used.
        """
        q = question_text.strip()

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
        all_descs = " ".join(descs)

        # Match question against rules table
        for pattern, answer_type in _PARAGRAPH_RULES:
            if not pattern.search(q):
                continue

            if answer_type == "technical_experience":
                # Use most recent experience description
                return first_desc or self._default_experience_answer()

            if answer_type == "product_experience":
                # Skills + relevant experience
                parts = []
                if _skills_sentence():
                    parts.append(_skills_sentence())
                if first_desc:
                    parts.append(first_desc)
                return " ".join(parts) or self._default_experience_answer()

            if answer_type == "decision_experience":
                # Second experience description for variety, or first
                if len(descs) >= 2:
                    return descs[1]
                return first_desc or self._default_experience_answer()

            if answer_type == "collaboration_experience":
                # Combine descriptions that mention team/collab keywords
                collab_kw = re.compile(r"team|collab|cross|partner|stakeholder|work\s+with", re.IGNORECASE)
                collab = [d for d in descs if collab_kw.search(d)]
                if collab:
                    return collab[0]
                return first_desc or self._default_experience_answer()

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
                return " ".join(parts) or self._default_experience_answer()

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
                return " ".join(parts) or self._default_experience_answer()

            if answer_type == "skills_experience":
                parts = []
                if _skills_sentence():
                    parts.append(_skills_sentence())
                if first_desc:
                    parts.append(first_desc)
                return " ".join(parts) or self._default_experience_answer()

            if answer_type == "achievement":
                # Look for descriptions with result-oriented keywords
                result_kw = re.compile(
                    r"achiev|result|deliver|impact|improv|reduc|increas|launch|build|led|drove",
                    re.IGNORECASE,
                )
                achievement_descs = [d for d in descs if result_kw.search(d)]
                if achievement_descs:
                    return achievement_descs[0]
                return first_desc or self._default_experience_answer()

        # No rule matched — use generic most-recent experience fallback
        return first_desc or None

    def _default_experience_answer(self) -> Optional[str]:
        """Return the first available experience description as a generic fallback."""
        for exp in (self.resume.experience or []):
            if exp.description:
                return exp.description.strip()
        # Last resort: skills sentence
        skills = self.resume.skills or []
        if skills:
            top = skills[:5]
            if len(top) > 1:
                return "I have strong experience with " + ", ".join(top[:-1]) + " and " + top[-1] + "."
            return "I have strong experience with " + top[0] + "."
        return None

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
           These have no ``<input>`` backing — just styled buttons with
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