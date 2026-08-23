"""Visible, step-by-step auto-fill animation engine for job application automation.

This module provides a human-visible, step-by-step automation experience:
1. Smoothly scrolls viewport to center each field with ease-in-out curve.
2. Focuses and visually highlights the active field with glowing blue border.
3. Renders a live on-screen floating HUD showing current field, step counter, and progress bar.
4. Types values character-by-character at a configurable human pace.
5. Flashes green success glow upon completion and pauses so the candidate can visually confirm.
6. Seamlessly handles text inputs, comboboxes, radios, checkboxes, and resume file uploads.
7. Gracefully handles manual intervention without skipping fields.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Any, Optional

from playwright.sync_api import Locator, Page


@dataclass
class VisibleMotionConfig:
    """Configuration for human-visible form filling animation."""
    type_delay_ms: int = 0
    field_start_pause_ms: int = 300
    field_complete_pause_ms: int = 600
    scroll_duration_ms: int = 600
    scroll_settle_ms: int = 300
    enable_hud: bool = False


class VisibleFormFiller:
    """Manages sequential, step-by-step visual animation of form filling."""

    def __init__(self, page: Page, config: Optional[VisibleMotionConfig] = None) -> None:
        self.page = page
        self.config = config or VisibleMotionConfig()
        self.current_field_index = 0
        self.total_fields = 0
        self.current_field_name = ""
        self._hud_injected = False

    def inject_hud(self, total_fields: int = 0) -> None:
        """Disabled: no on-screen overlay HUD."""
        return

    def update_hud(
        self,
        current_index: int,
        field_name: str,
        status: str,
        is_complete: bool = False,
        is_manual: bool = False,
    ) -> None:
        """Disabled: no on-screen overlay HUD."""
        return

    def remove_hud(self) -> None:
        """Remove floating HUD overlay from DOM if any exists."""
        try:
            self.page.evaluate("() => { const el = document.getElementById('jobcli-autofill-hud'); if (el) el.remove(); }")
            self._hud_injected = False
        except Exception:
            pass
            pass

    # -------------------------------------------------------------------------
    # Smooth Viewport Scrolling
    # -------------------------------------------------------------------------

    def scroll_to_field(self, locator_or_selector: Any, duration_ms: Optional[int] = None) -> None:
        """Smoothly glide the viewport to center the target element with cubic bezier easing."""
        duration = duration_ms or self.config.scroll_duration_ms
        js = f"""(el) => {{
            if (!el) return;
            const rect = el.getBoundingClientRect();
            const targetY = window.scrollY + rect.top - (window.innerHeight / 2) + (rect.height / 2);
            const startY = window.scrollY;
            const diff = targetY - startY;
            if (Math.abs(diff) < 15) return;

            const duration = {duration};
            const startTime = performance.now();

            return new Promise(resolve => {{
                function step(currentTime) {{
                    const elapsed = currentTime - startTime;
                    const progress = Math.min(elapsed / duration, 1);
                    const ease = progress < 0.5
                        ? 4 * progress * progress * progress
                        : 1 - Math.pow(-2 * progress + 2, 3) / 2;
                    window.scrollTo(0, startY + (diff * ease));
                    if (progress < 1) {{
                        requestAnimationFrame(step);
                    }} else {{
                        resolve();
                    }}
                }}
                requestAnimationFrame(step);
            }});
        }}"""
        try:
            if hasattr(locator_or_selector, "evaluate"):
                locator_or_selector.evaluate(js)
            elif isinstance(locator_or_selector, str):
                el = self.page.query_selector(locator_or_selector)
                if el:
                    el.evaluate(js)
            self.page.wait_for_timeout(duration + self.config.scroll_settle_ms)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Visual Highlights (Active vs. Completed)
    # -------------------------------------------------------------------------

    def highlight_active(self, locator_or_selector: Any) -> None:
        """Apply active blue focus glow to element."""
        js = """(el) => {
            if (!el) return;
            el.style.transition = 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)';
            el.style.border = '2px solid #3b82f6';
            el.style.boxShadow = '0 0 0 4px rgba(59, 130, 246, 0.25)';
            el.style.backgroundColor = '#eff6ff';
        }"""
        try:
            if hasattr(locator_or_selector, "evaluate"):
                locator_or_selector.evaluate(js)
            elif isinstance(locator_or_selector, str):
                el = self.page.query_selector(locator_or_selector)
                if el:
                    el.evaluate(js)
        except Exception:
            pass

    def highlight_completed(self, locator_or_selector: Any) -> None:
        """Flash emerald green confirmation glow on completed field."""
        js = """(el) => {
            if (!el) return;
            el.style.border = '2px solid #10b981';
            el.style.boxShadow = '0 0 0 4px rgba(16, 185, 129, 0.3)';
            el.style.backgroundColor = '#ecfdf5';
        }"""
        try:
            if hasattr(locator_or_selector, "evaluate"):
                locator_or_selector.evaluate(js)
            elif isinstance(locator_or_selector, str):
                el = self.page.query_selector(locator_or_selector)
                if el:
                    el.evaluate(js)
            self.page.wait_for_timeout(self.config.field_complete_pause_ms)
            self.reset_highlight(locator_or_selector)
        except Exception:
            pass

    def reset_highlight(self, locator_or_selector: Any) -> None:
        """Gracefully remove temporary inline highlight styling."""
        js = """(el) => {
            if (!el) return;
            el.style.border = '';
            el.style.boxShadow = '';
            el.style.backgroundColor = '';
        }"""
        try:
            if hasattr(locator_or_selector, "evaluate"):
                locator_or_selector.evaluate(js)
            elif isinstance(locator_or_selector, str):
                el = self.page.query_selector(locator_or_selector)
                if el:
                    el.evaluate(js)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Core Step-by-Step Field Filling
    # -------------------------------------------------------------------------

    def fill_field_visibly(
        self,
        locator: Locator,
        value: str,
        field_name: str = "Input Field",
        field_index: Optional[int] = None,
    ) -> bool:
        """Fill a text input or textarea with slow-motion visible typing animation.

        1. Check visibility & interactability.
        2. Smoothly scroll into viewport center.
        3. Apply active focus glow (blue ring).
        4. Clear existing value.
        5. Type character-by-character at human pace.
        6. Dispatch change & input events.
        7. Flash green completed highlight.
        8. Pause so the candidate visually confirms.
        """
        if not value:
            return False

        try:
            if not locator.is_visible(timeout=1500) or not locator.is_enabled(timeout=1500):
                return False
        except Exception:
            return False

        idx = field_index or (self.current_field_index + 1)
        self.update_hud(idx, field_name, f"Typing \"{value[:25]}{'...' if len(value) > 25 else ''}\"...")

        try:
            # 1. Smooth scroll element into view
            self.scroll_to_field(locator)

            # 2. Focus and active blue highlight
            self.highlight_active(locator)
            locator.focus(timeout=1500)
            self.page.wait_for_timeout(self.config.field_start_pause_ms)

            # 3. Clean, reliable direct fill
            locator.fill(value)

            # 4. Dispatch input & change events for React/Angular bindings
            locator.evaluate("""el => {
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""")

            # 5. Flash green completed state & pause
            self.highlight_completed(locator)
            return True

        except Exception as e:
            try:
                locator.fill(value)
                self.reset_highlight(locator)
                return True
            except Exception:
                return False

    def fill_combobox_visibly(
        self,
        question_text: str,
        value: str,
        field_index: Optional[int] = None,
    ) -> bool:
        """Visually select an option in a combobox or searchable dropdown."""
        if not question_text or not value:
            return False

        idx = field_index or (self.current_field_index + 1)
        self.update_hud(idx, question_text, f"Selecting \"{value}\"...")

        try:
            js = r"""(args) => {
                const q = (args.question || '').toLowerCase().trim();
                const v = (args.value || '').toLowerCase().trim();
                if (!q || !v) return false;

                const labels = [
                    ...document.querySelectorAll('label'),
                    ...document.querySelectorAll('legend'),
                    ...document.querySelectorAll('[class*=field-label]'),
                    ...document.querySelectorAll('[class*=question-title]')
                ];

                for (const label of labels) {
                    const txt = (label.innerText || '').toLowerCase();
                    if (!txt.includes(q)) continue;

                    let container = label.parentElement;
                    while (container) {
                        const combo = container.querySelector(
                            '[role="combobox"], button[aria-haspopup="listbox"], ' +
                            'button[aria-haspopup="true"], input[role="combobox"], select, input[placeholder*="location" i]'
                        );
                        if (combo) {
                            combo.scrollIntoView({ behavior: 'smooth', block: 'center' });
                            combo.style.transition = 'all 0.3s ease';
                            combo.style.border = '2px solid #3b82f6';
                            combo.style.boxShadow = '0 0 0 4px rgba(59, 130, 246, 0.25)';

                            if (combo.tagName === 'SELECT') {
                                const options = [...combo.options];
                                const match = options.find(o =>
                                    o.text.toLowerCase().trim() === v ||
                                    o.value.toLowerCase().trim() === v ||
                                    o.text.toLowerCase().includes(v)
                                );
                                if (match) {
                                    combo.value = match.value;
                                    combo.dispatchEvent(new Event('change', { bubbles: true }));
                                    combo.style.border = '2px solid #10b981';
                                    combo.style.boxShadow = '0 0 0 4px rgba(16, 185, 129, 0.3)';
                                    return true;
                                }
                            } else {
                                combo.click();
                                return new Promise(resolve => {
                                    setTimeout(() => {
                                        const options = [...document.querySelectorAll('[role="option"], [class*="option"], [class*="suggestion"]')];
                                        const match = options.find(o =>
                                            (o.innerText || '').toLowerCase().trim() === v
                                        );
                                        const target = match || options.find(o => (o.innerText || '').toLowerCase().includes(v)) || (options.length > 0 ? options[0] : null);
                                        if (target) {
                                            target.style.transition = 'all 0.2s ease';
                                            target.style.backgroundColor = '#ecfdf5';
                                            target.style.border = '1px solid #10b981';
                                            setTimeout(() => {
                                                target.click();
                                                combo.style.border = '2px solid #10b981';
                                                combo.style.boxShadow = '0 0 0 4px rgba(16, 185, 129, 0.3)';
                                                setTimeout(() => {
                                                    combo.style.border = '';
                                                    combo.style.boxShadow = '';
                                                }, 500);
                                                resolve(true);
                                            }, 200);
                                        } else {
                                            resolve(false);
                                        }
                                    }, 400);
                                });
                            }
                        }
                        container = container.parentElement;
                    }
                }
                return false;
            }"""
            result = self.page.evaluate(js, {"question": question_text, "value": value})
            if result:
                self.update_hud(idx, question_text, f"✓ Selected \"{value}\"", is_complete=True)
                self.page.wait_for_timeout(self.config.field_complete_pause_ms)
                return True
        except Exception:
            pass
        return False

    def select_option_visibly(
        self,
        question_text: str,
        value: str,
        field_index: Optional[int] = None,
    ) -> bool:
        """Visually select a radio button, checkbox, or button-segmented option."""
        if not question_text or not value:
            return False

        idx = field_index or (self.current_field_index + 1)
        self.update_hud(idx, question_text, f"Selecting \"{value}\"...")

        try:
            js = r"""(args) => {
                const q = (args.q || '').toLowerCase().trim();
                const v = (args.v || '').toLowerCase().trim();
                if (!q || !v) return false;

                // Match fieldsets and question groups
                const groups = document.querySelectorAll('fieldset, [role="radiogroup"], [role="group"], [class*="field-entry"], [class*="form-field"]');
                for (const grp of groups) {
                    const title = grp.querySelector('legend, label, [class*="title"], [class*="label"], strong, h3, h4, p');
                    const text = title ? title.innerText.toLowerCase() : '';
                    if (!text.includes(q)) continue;

                    grp.scrollIntoView({ behavior: 'smooth', block: 'center' });

                    const options = grp.querySelectorAll('input[type="radio"], input[type="checkbox"], [role="radio"], [role="checkbox"], button');
                    for (const opt of options) {
                        const lbl = opt.closest('label') || opt.parentElement;
                        const optText = (opt.innerText || (lbl ? lbl.innerText : '') || opt.getAttribute('value') || '').toLowerCase().trim();
                        if (optText === v || optText.includes(v)) {
                            const target = lbl || opt;
                            target.style.transition = 'all 0.3s ease';
                            target.style.outline = '2px solid #10b981';
                            target.style.boxShadow = '0 0 0 4px rgba(16, 185, 129, 0.3)';
                            target.click();
                            return true;
                        }
                    }
                }

                // Direct search for labeled options
                const allLabels = document.querySelectorAll('label, [class*="option"], button');
                for (const lbl of allLabels) {
                    const txt = (lbl.innerText || '').toLowerCase().trim();
                    if (txt === v) {
                        lbl.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        lbl.style.transition = 'all 0.3s ease';
                        lbl.style.outline = '2px solid #10b981';
                        lbl.style.boxShadow = '0 0 0 4px rgba(16, 185, 129, 0.3)';
                        lbl.click();
                        return true;
                    }
                }
                return false;
            }"""
            result = self.page.evaluate(js, {"q": question_text, "v": value})
            if result:
                self.update_hud(idx, question_text, f"✓ Selected \"{value}\"", is_complete=True)
                self.page.wait_for_timeout(self.config.field_complete_pause_ms)
                return True
        except Exception:
            pass
        return False

    def upload_resume_visibly(
        self,
        file_input_locator: Locator,
        pdf_path: str,
        field_index: Optional[int] = None,
    ) -> bool:
        """Visually upload resume file with smooth scrolling and completion confirmation."""
        if not pdf_path:
            return False

        idx = field_index or (self.current_field_index + 1)
        self.update_hud(idx, "Resume Upload", "Uploading resume document...")

        try:
            self.scroll_to_field(file_input_locator)
            self.highlight_active(file_input_locator)
            self.page.wait_for_timeout(self.config.field_start_pause_ms)

            file_input_locator.set_input_files(pdf_path)
            self.page.wait_for_timeout(1800)  # Wait for upload checkmark/indicator

            self.update_hud(idx, "Resume Upload", "✓ Resume uploaded successfully", is_complete=True)
            self.highlight_completed(file_input_locator)
            return True
        except Exception:
            return False

    def prompt_manual_intervention(
        self,
        locator_or_selector: Any,
        field_name: str,
        reason: str = "Please complete this field.",
    ) -> None:
        """Pause automation and alert candidate to complete a required manual field."""
        self.scroll_to_field(locator_or_selector)
        js = """(el) => {
            if (!el) return;
            el.style.transition = 'all 0.3s ease';
            el.style.border = '2px solid #f59e0b';
            el.style.boxShadow = '0 0 0 4px rgba(245, 158, 11, 0.3)';
            el.style.backgroundColor = '#fffbeb';
        }"""
        try:
            if hasattr(locator_or_selector, "evaluate"):
                locator_or_selector.evaluate(js)
            elif isinstance(locator_or_selector, str):
                el = self.page.query_selector(locator_or_selector)
                if el:
                    el.evaluate(js)
        except Exception:
            pass

        self.update_hud(
            self.current_field_index,
            field_name,
            f"⚠️ Manual Input Required: {reason}",
            is_manual=True
        )
