"""Human-like interaction helpers for visible, step-by-step form filling."""

from typing import Union
from playwright.sync_api import Frame, Locator, Page

from jobcli.utils.fill_guard import should_skip_refill


def humanized_fill(page: Union[Page, Frame], locator: Locator, value: str) -> bool:
    """Type into a field with smooth visible animation and human-like cadence.
    
    1. Smoothly scroll into viewport center.
    2. Apply glowing blue active highlight.
    3. Type character-by-character with natural typing variation (45-75ms).
    4. Flash emerald green completed highlight.
    5. Pause briefly so candidate can visually confirm.
    """
    import random as _r

    if hasattr(page, "page"):
        page = page.page

    if not value:
        return False

    if should_skip_refill(locator, value):
        return False

    try:
        # 1. Smooth scroll element into view (center block)
        locator.evaluate("""el => {
            el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        }""")
        page.wait_for_timeout(350)

        # 2. Active blue focus highlight
        locator.evaluate("""el => {
            el.style.transition = 'all 0.3s cubic-bezier(0.4, 0, 0.2, 1)';
            el.style.border = '2px solid #3b82f6';
            el.style.boxShadow = '0 0 0 4px rgba(59, 130, 246, 0.25)';
            el.style.backgroundColor = '#eff6ff';
        }""")
        locator.focus(timeout=1500)
        page.wait_for_timeout(250)

        # 3. Clear existing value
        locator.fill("")

        # 4. Type character by character
        for char in value:
            locator.type(char, delay=_r.randint(45, 75))

        # 5. Dispatch events
        locator.evaluate("""el => {
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""")

        # 6. Green completed highlight & confirmation pause
        locator.evaluate("""el => {
            el.style.border = '2px solid #10b981';
            el.style.boxShadow = '0 0 0 4px rgba(16, 185, 129, 0.3)';
            el.style.backgroundColor = '#ecfdf5';
        }""")
        page.wait_for_timeout(500)

        # 7. Reset style gracefully
        locator.evaluate("""el => {
            el.style.border = '';
            el.style.boxShadow = '';
            el.style.backgroundColor = '';
        }""")
        page.wait_for_timeout(200)
        return True

    except Exception:
        try:
            locator.fill(value)
            return True
        except Exception:
            return False
