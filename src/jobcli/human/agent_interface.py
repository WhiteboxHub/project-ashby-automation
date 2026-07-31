"""Unified agent interface — integrates human-in-the-loop inline with auto-apply.

Instead of a separate "human phase", this interface is called at checkpoints
throughout the agent loop: the agent runs
autonomously but pauses at key moments for human review / input / confirmation.

Key behaviours:
  * Every human-facing prompt first checks ``AgentMemory`` for a similar
    answer.  If found, it is used silently (or shown as default).
  * Every answer the human gives is persisted to the DB via ``AgentMemory``
    with source="human", so the next job's agent loop will reuse it.
  * When asking, the agent loop is expected to STOP filling other fields
    until ``request_field_input()`` returns — see ``_agent_fill_loop`` in
    engine.py for the wait-and-re-scan pattern.
"""

import os
import sys
import time
import threading
import queue
from io import StringIO
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from jobcli.utils.browser_closed import (
    BROWSER_CLOSED_SENTINEL,
    BrowserClosed,
    is_playwright_page_closed,
)

# Returned from _get_user_input when live submit detection fires during handoff.
SUBMITTED_SENTINEL = "__JOBCLI_SUBMITTED__"
from jobcli.utils.url_compare import urls_meaningfully_different

from playwright.sync_api import Page
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from jobcli.utils.logger import JobLogger
from jobcli.ats.schemas.locator_schemas import LearnedLocator
from jobcli.profile.schemas import (
    ATSType,
    BrowserAction,
    ActionType,
    ExecutionPhase,
    InteractionMode,
    ResumeData,
    SelectorType,
    CommonQuestions,
)
from jobcli.storage.repositories import LearnedLocatorRepository

if TYPE_CHECKING:
    from jobcli.intelligence.memory import AgentMemory


@dataclass
class HandoffResult:
    """Result returned after a human takes manual control of the browser.

    The engine uses this to decide whether to *resume from the human's current
    page* (skipping any phase the human already completed) instead of retrying
    whatever the agent originally got stuck on.
    """

    page: Page                 # current page reference (may be a new tab)
    url_before: str            # URL when handoff started
    url_after: str             # URL when human handed control back
    title_after: str           # title of current page after handoff
    advanced: bool             # True if human navigated to a different URL
    cancelled: bool = False    # human chose to abort the application
    skipped: bool = False      # human chose to skip this job
    submitted: bool = False    # confirmation page detected during handoff


# ------------------------------------------------------------------
# ProxyConsole: Captures Rich output and relays it to the API
# ------------------------------------------------------------------

class ProxyConsole:
    """Wraps rich.console.Console to capture ANSI output for the web dashboard."""
    def __init__(self, logger=None):
        # The real terminal console
        self._real_console = Console()
        # A virtual console to capture ANSI-formatted strings
        self._capture_buffer = StringIO()
        self._capture_console = Console(
            file=self._capture_buffer,
            force_terminal=True,
            color_system="standard", # standard is safer for many terminal emulators
            width=min(self._real_console.width or 80, 80), # Cap width for UI dashboard
        )
        self.logger = logger

    def print(self, *args, **kwargs) -> None:
        """Print to local terminal AND capture for the web UI."""
        # 1. Real output
        self._real_console.print(*args, **kwargs)
        
        # 2. Capture output
        self._capture_buffer.seek(0)
        self._capture_buffer.truncate(0)
        self._capture_console.print(*args, **kwargs)
        ansi_text = self._capture_buffer.getvalue()
        
        # 3. Relay to logger
        if self.logger and hasattr(self.logger, "emit_event"):
            # We emit a 'terminal' event for high-fidelity output.
            # We SKIP calling logger.info here to avoid double-logs in the UI.
            self.logger.emit_event({"type": "terminal", "message": ansi_text})

    @property
    def width(self) -> int:
        return self._real_console.width or 80


class AgentInterface:
    """Single entry-point for every human-facing interaction during the agent loop.

    Behaviour adapts to the current ``InteractionMode``:
    * AUTO       – never blocks; returns safe defaults.
    * SUPERVISED – blocks only at safety-critical checkpoints (submit, captcha,
                   missing mandatory info, low-confidence actions).
    * MANUAL     – blocks at every checkpoint for explicit approval.
    """
    def __init__(
        self,
        page: Page,
        locator_repo: LearnedLocatorRepository,
        mode: InteractionMode = InteractionMode.SUPERVISED,
        logger: Optional[JobLogger] = None,
        memory: Optional["AgentMemory"] = None,
        resume: Optional[ResumeData] = None,
        ats_type: ATSType = ATSType.UNKNOWN,
        is_server: bool = False,
    ) -> None:
        self.page = page
        self.locator_repo = locator_repo
        self.mode = mode
        self.logger = logger
        self.memory = memory
        self.resume = resume
        self.ats_type = ats_type
        self.is_server = is_server
        self.common_questions = None
        self.console = ProxyConsole(logger=logger)
        # Track which (label, value) pairs we already saved this session — avoids
        # re-saving the same answer many times during multi-page form loops.
        self._saved_this_session: set[tuple[str, str]] = set()
        # Session-level question answer cache: maps normalized question text →
        # the answer given during the CURRENT application run.  This lets the
        # agent reuse answers for repeated questions that appear on multiple
        # pages / steps of a multi-step ATS form *within the same session*,
        # even before the DB confidence gate is met.  It is intentionally
        # NOT persisted across runs (that is the DB memory's job).
        self._session_question_cache: dict[str, str] = {}

        # Remote interaction support
        self._input_event = threading.Event()
        self._input_value: Optional[str] = None
        self._is_waiting = False

    def remote_resume(self, value: str) -> None:
        """Signal the waiting agent to resume with the given value."""
        if self._is_waiting:
            self._input_value = value
            self._input_event.set()

    def _get_user_input(
        self,
        prompt_text: str,
        default: str = "",
        timeout_seconds: Optional[int] = None,
        *,
        poll_browser_fields: bool = False,
        submission_checker: Optional[Callable[[], tuple[bool, bool, dict]]] = None,
    ) -> Optional[str]:
        """Wait for input from either the local terminal or a remote signal.

        Returns the typed text (or ``default`` on empty input). Returns
        ``None`` only on local-terminal timeout when ``timeout_seconds`` is
        set.

        Universal quit / Ctrl+C semantics
        ---------------------------------
        Typing any of the canonical quit keywords (``q``, ``quit``,
        ``exit``, ``:q``, ``quit-all``) or pressing Ctrl+C raises
        :class:`jobcli.utils.exit_signal.ExitRequested`, which bubbles up
        through the engine to the top-level apply loop and triggers a
        graceful shutdown (close browser, persist state, exit 0).

        We register our event with the global exit registry so a Ctrl+C
        handled by the process-wide SIGINT handler can wake this prompt
        immediately — Windows PowerShell will otherwise leave a daemon
        thread blocked on ``input()`` until the user types something.
        """
        from jobcli.utils.exit_signal import (
            ExitRequested,
            is_exit_requested,
            is_quit_keyword,
            register_input_event,
            unregister_input_event,
        )

        browser_poll_stop = threading.Event()
        last_field_snapshot: dict[str, str] = {}
        submit_detected = threading.Event()

        def _poll_browser_closed() -> None:
            while not browser_poll_stop.is_set() and self._is_waiting:
                if is_playwright_page_closed(self.page):
                    self.remote_resume(BROWSER_CLOSED_SENTINEL)
                    return
                browser_poll_stop.wait(0.5)

        def _poll_browser_field_sync() -> None:
            from jobcli.utils.form_sync import diff_snapshots, snapshot_field_values

            nonlocal last_field_snapshot
            if not poll_browser_fields or not self.page:
                return
            try:
                last_field_snapshot = snapshot_field_values(self.page)
            except Exception:
                last_field_snapshot = {}
            while not browser_poll_stop.is_set() and self._is_waiting:
                if submit_detected.is_set():
                    return
                try:
                    if is_playwright_page_closed(self.page):
                        return
                    current = snapshot_field_values(self.page)
                    for label, _old, new in diff_snapshots(last_field_snapshot, current):
                        preview = new if len(new) <= 80 else new[:77] + "…"
                        self.console.print(
                            f"  [dim]Browser updated:[/dim] [cyan]{label}[/cyan] "
                            f"= '{preview}'"
                        )
                        if self.logger:
                            try:
                                self.logger.emit_event({
                                    "type": "field_sync",
                                    "source": "browser",
                                    "label": label,
                                    "value": new,
                                })
                            except Exception:
                                pass
                    last_field_snapshot = current
                except Exception:
                    pass
                browser_poll_stop.wait(0.5)

        def _poll_submission() -> None:
            if not submission_checker or not self.page:
                return
            while not browser_poll_stop.is_set() and self._is_waiting:
                if submit_detected.is_set():
                    return
                try:
                    strong, _soft, _signals = submission_checker()
                    if strong:
                        submit_detected.set()
                        self.clear_browser_overlay()
                        self.console.print(
                            "  [bold green]OK[/bold green] Application submitted in browser "
                            "— detected confirmation page."
                        )
                        if self.logger:
                            try:
                                self.logger.emit_event({
                                    "type": "submission_detected",
                                    "source": "browser",
                                })
                            except Exception:
                                pass
                        self.remote_resume(SUBMITTED_SENTINEL)
                        return
                except Exception:
                    pass
                browser_poll_stop.wait(0.5)

        self._is_waiting = True
        self._input_event.clear()
        self._input_value = None

        # If the global SIGINT handler already fired (e.g. user pressed
        # Ctrl+C before this prompt even appeared), short-circuit.
        if is_exit_requested():
            self._is_waiting = False
            raise ExitRequested("Ctrl+C received before prompt")

        if self.logger:
            self.logger.info(f"Agent waiting for input: {prompt_text}", phase=ExecutionPhase.HUMAN)
            # Notify the dashboard explicitly that input is required
            try:
                self.logger.emit_event({
                    "type": "human_input_required",
                    "prompt": prompt_text,
                    "default": default,
                    "timeout": timeout_seconds
                })
            except Exception:
                pass

        # Make sure the prompt text is relayed over WebSocket to the dashboard UI
        self.console.print(prompt_text, end="")

        # Register so global SIGINT can wake us mid-wait. Always paired with
        # an unregister in finally so we don't leak events across calls.
        register_input_event(self._input_event)

        try:
            if self.is_server:
                if poll_browser_fields:
                    threading.Thread(
                        target=_poll_browser_field_sync, daemon=True
                    ).start()
                if submission_checker:
                    threading.Thread(target=_poll_submission, daemon=True).start()
                threading.Thread(target=_poll_browser_closed, daemon=True).start()
                # Server mode ignores the timeout parameter for now, as the dashboard handles its own state
                self._input_event.wait()
            else:
                # CLI: read stdin on a helper thread, but poll Playwright only on
                # the main thread — background page access causes greenlet errors
                # on Windows and can hang skip/cancel.
                def local_input_thread():
                    try:
                        val = input()
                        self.remote_resume(val)
                    except (EOFError, KeyboardInterrupt):
                        self.remote_resume("__JOBCLI_EXIT__")

                threading.Thread(target=local_input_thread, daemon=True).start()

                if poll_browser_fields and self.page:
                    try:
                        from jobcli.utils.form_sync import snapshot_field_values

                        last_field_snapshot = snapshot_field_values(self.page)
                    except Exception:
                        last_field_snapshot = {}

                deadline = (
                    time.monotonic() + timeout_seconds
                    if timeout_seconds is not None
                    else None
                )

                def _cli_poll_playwright_once() -> None:
                    nonlocal last_field_snapshot
                    if is_playwright_page_closed(self.page):
                        self.remote_resume(BROWSER_CLOSED_SENTINEL)
                        return
                    if poll_browser_fields and self.page and not submit_detected.is_set():
                        try:
                            from jobcli.utils.form_sync import (
                                diff_snapshots,
                                snapshot_field_values,
                            )

                            current = snapshot_field_values(self.page)
                            for label, _old, new in diff_snapshots(
                                last_field_snapshot, current
                            ):
                                preview = new if len(new) <= 80 else new[:77] + "…"
                                self.console.print(
                                    f"  [dim]Browser updated:[/dim] [cyan]{label}[/cyan] "
                                    f"= '{preview}'"
                                )
                            last_field_snapshot = current
                        except Exception:
                            pass
                    if submission_checker and self.page and not submit_detected.is_set():
                        try:
                            strong, _soft, _signals = submission_checker()
                            if strong:
                                submit_detected.set()
                                self.clear_browser_overlay()
                                self.console.print(
                                    "  [bold green]OK[/bold green] Application submitted "
                                    "in browser — detected confirmation page."
                                )
                                self.remote_resume(SUBMITTED_SENTINEL)
                        except Exception:
                            pass

                while not self._input_event.is_set():
                    if is_exit_requested():
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        self._is_waiting = False
                        return None
                    _cli_poll_playwright_once()
                    if self._input_event.is_set():
                        break
                    self._input_event.wait(0.4)
        finally:
            browser_poll_stop.set()
            unregister_input_event(self._input_event)

        self._is_waiting = False

        # The SIGINT handler may have woken us with no value written.
        if is_exit_requested() and not self._input_value:
            raise ExitRequested("Ctrl+C during input wait")

        raw = self._input_value
        if raw == BROWSER_CLOSED_SENTINEL:
            raise BrowserClosed("User closed the browser window")
        if raw == SUBMITTED_SENTINEL:
            return SUBMITTED_SENTINEL
        if raw == "__JOBCLI_EXIT__":
            raise ExitRequested("Ctrl+C / EOF on stdin")
        if is_quit_keyword(raw):
            raise ExitRequested(f"quit keyword '{(raw or '').strip()}' at prompt")

        return raw or default

    # ------------------------------------------------------------------
    # DB integration helpers (memory-aware)
    # ------------------------------------------------------------------

    def set_context(
        self,
        *,
        memory: Optional["AgentMemory"] = None,
        resume: Optional[ResumeData] = None,
        ats_type: Optional[ATSType] = None,
        common_questions: Optional[CommonQuestions] = None,
    ) -> None:
        """Update memory / resume / ats_type after construction (e.g. once detected)."""
        if memory is not None:
            self.memory = memory
        if resume is not None:
            self.resume = resume
        if ats_type is not None:
            self.ats_type = ats_type
        if common_questions is not None:
            self.common_questions = common_questions

    @staticmethod
    def _strip_prompt_markup(field_label: str) -> str:
        """Remove Rich markup suffixes from labels used in terminal prompts."""
        import re

        return re.sub(r"\[/?[^\]]+\]", "", field_label or "").strip()

    def _read_browser_field_value(self, field_label: str) -> Optional[str]:
        """Return the live DOM value for *field_label*, if the user already filled it."""
        from jobcli.utils.fill_guard import is_meaningful_value, read_locator_value

        clean = self._strip_prompt_markup(field_label)
        if not clean or not self.page:
            return None
        candidates: list = []
        try:
            candidates.append(self.page.get_by_label(clean, exact=False).first)
        except Exception:
            pass
        try:
            candidates.append(self.page.get_by_placeholder(clean, exact=False).first)
        except Exception:
            pass
        try:
            candidates.append(
                self.page.get_by_role("textbox", name=clean, exact=False).first
            )
        except Exception:
            pass
        try:
            candidates.append(
                self.page.get_by_role("combobox", name=clean, exact=False).first
            )
        except Exception:
            pass
        for loc in candidates:
            val = read_locator_value(loc)
            if is_meaningful_value(val):
                return val
        return None

    def lookup_db_answer(self, field_label: str) -> tuple[Optional[str], str]:
        """Check the DB for a previously-saved answer to a similar question.

        Uses ``AgentMemory.get_best_answer`` which already does:
          1. resume JSON match
          2. saved memory for THIS ATS
          3. universal saved memory across all ATSes
        Returns ``(value, source)`` or ``(None, "not_found")``.
        """
        from jobcli.utils.fill_guard import is_reserved_form_value

        if not self.memory or not field_label:
            return None, "not_found"
        value, source = self.memory.get_best_answer(
            self._strip_prompt_markup(field_label),
            self.ats_type,
            self.resume,
            common_questions=getattr(self, "common_questions", None),
        )
        if value and is_reserved_form_value(value):
            return None, "not_found"
        return value, source

    def resolve_memory_silent(self, field_label: str) -> tuple[Optional[str], str]:
        """DB lookup without terminal prompt — safe for AUTO mode."""
        return self.lookup_db_answer(field_label)

    def persist_human_answer(self, field_label: str, value: str) -> bool:
        """Save a human-supplied answer to the DB so future jobs can reuse it."""
        from jobcli.utils.fill_guard import is_reserved_form_value

        if not self.memory or not field_label or not value:
            return False
        if is_reserved_form_value(value):
            return False
        key = (field_label.strip().lower(), value.strip())
        if key in self._saved_this_session:
            return False
        self._saved_this_session.add(key)
        saved = self.memory.save_field_answer(
            field_label, value, self.ats_type, success=True, source="human"
        )
        # Cross-ATS reuse for generic questions (notice period, salary, etc.)
        if saved and self.memory.synonym_resolver.resolve_field_label(field_label):
            self.memory.save_field_answer(
                field_label, value, ATSType.UNKNOWN, success=True, source="human"
            )
        if saved and self.logger:
            self.logger.info(
                f"Saved human answer for '{field_label}' to DB.",
                phase=ExecutionPhase.HUMAN,
            )
        return saved

    # ------------------------------------------------------------------
    # Browser-side attention helpers
    # ------------------------------------------------------------------
    #
    # When the terminal needs the human, the human is almost certainly
    # looking at the BROWSER, not the terminal.  These helpers make the
    # request impossible to miss:
    #
    #   * inject a top-of-page yellow banner overlay into the live page
    #   * bring the browser window to the foreground (Mac: dock bounce)
    #   * ring the terminal bell (most terminals flash + dock-bounce)
    #   * (best-effort) show a macOS system notification
    #
    # All steps are wrapped in try/except so headless / closed pages /
    # cross-origin frames never break the agent loop.

    _OVERLAY_ID = "__jobcli_handoff_overlay__"
    _OVERLAY_STORAGE_KEY = "jobcli_handoff_overlay_pos"

    def show_browser_overlay(
        self,
        title: str,
        message: str,
        *,
        kind: str = "warning",  # "warning" | "info" | "error"
        fields: Optional[list[str]] = None,
    ) -> None:
        """Inject a draggable handoff banner into the browser page.

        Default: full-width bar at the top. The user can drag it by the handle
        to reposition (position is remembered per origin in sessionStorage).

        Rendered inside Shadow DOM so ATS page CSS cannot affect it.
        Removed by ``clear_browser_overlay()`` or on navigation.
        """
        try:
            color = {
                "info":    {"bg": "#1d4ed8", "border": "#1e3a8a"},   # blue
                "warning": {"bg": "#f59e0b", "border": "#b45309"},   # amber
                "error":   {"bg": "#dc2626", "border": "#7f1d1d"},   # red
            }.get(kind, {"bg": "#f59e0b", "border": "#b45309"})

            field_list_html = ""
            if fields:
                items = "".join(f"<li>{self._escape_html(f)}</li>" for f in fields[:8])
                if len(fields) > 8:
                    items += f"<li>… and {len(fields) - 8} more</li>"
                field_list_html = f"<ul class='fields'>{items}</ul>"

            self.page.evaluate(
                r"""({id, title, message, color, fieldsHtml, storageKey}) => {
                    const old = document.getElementById(id);
                    if (old) old.remove();

                    const host = document.createElement('div');
                    host.id = id;
                    host.setAttribute('data-jobcli', 'handoff');

                    const shadow = host.attachShadow({mode: 'closed'});

                    const css = `
                        :host, * { all: revert; }
                        :host {
                            display: block;
                            font: 15px/1.5 -apple-system, BlinkMacSystemFont,
                                  'Segoe UI', Roboto, 'Helvetica Neue',
                                  Arial, sans-serif;
                            letter-spacing: normal;
                            word-spacing: normal;
                            text-transform: none;
                            font-variant: normal;
                            font-feature-settings: normal;
                            font-style: normal;
                            font-weight: normal;
                            text-decoration: none;
                            text-shadow: none;
                            -webkit-font-smoothing: antialiased;
                            -moz-osx-font-smoothing: grayscale;
                            text-rendering: optimizeLegibility;
                            color: #ffffff;
                        }
                        .bar {
                            box-sizing: border-box;
                            width: 100%;
                            padding: 14px 20px;
                            background: ${color.bg};
                            color: #ffffff;
                            border-bottom: 4px solid ${color.border};
                            box-shadow: 0 4px 14px rgba(0,0,0,0.35);
                            animation: jobcli-pulse 1.4s ease-in-out infinite;
                        }
                        .inner {
                            max-width: 1200px;
                            margin: 0 auto;
                            display: flex;
                            align-items: flex-start;
                            gap: 14px;
                        }
                        .drag-handle {
                            flex: 0 0 auto;
                            cursor: grab;
                            user-select: none;
                            touch-action: none;
                            padding: 2px 6px;
                            margin-top: 2px;
                            opacity: 0.9;
                            font-size: 18px;
                            line-height: 1;
                            letter-spacing: -2px;
                        }
                        .drag-handle:active {
                            cursor: grabbing;
                        }
                        .icon {
                            font-size: 26px;
                            line-height: 1.1;
                            flex: 0 0 auto;
                        }
                        .body {
                            flex: 1 1 auto;
                            min-width: 0;
                        }
                        .title {
                            font-weight: 700;
                            font-size: 16px;
                            margin: 0 0 4px 0;
                            letter-spacing: normal;
                            line-height: 1.35;
                            word-break: normal;
                            overflow-wrap: anywhere;
                        }
                        .msg {
                            font-size: 14px;
                            line-height: 1.5;
                            margin: 0;
                            letter-spacing: normal;
                            word-break: normal;
                            overflow-wrap: anywhere;
                            opacity: 0.97;
                        }
                        .fields {
                            margin: 8px 0 0 20px;
                            padding: 0;
                            font-size: 13px;
                            line-height: 1.5;
                        }
                        .fields li {
                            margin: 2px 0;
                        }
                        .hint {
                            margin-top: 8px;
                            font-size: 12px;
                            line-height: 1.5;
                            opacity: 0.85;
                        }
                        @keyframes jobcli-pulse {
                            0%, 100% { box-shadow: 0 4px 14px rgba(0,0,0,0.35); }
                            50%      { box-shadow: 0 4px 22px ${color.bg}; }
                        }
                    `;

                    const style = document.createElement('style');
                    style.textContent = css;

                    const wrap = document.createElement('div');
                    wrap.className = 'bar';
                    wrap.innerHTML = `
                        <div class="inner">
                            <div class="drag-handle" title="Drag to move">⋮⋮</div>
                            <div class="icon">⏸</div>
                            <div class="body">
                                <div class="title"></div>
                                <div class="msg"></div>
                                <div class="fields-slot"></div>
                                <div class="hint">Drag the banner by the handle to move it. JobCLI is waiting in the terminal — finish here, then return to the terminal and press ENTER.</div>
                            </div>
                        </div>
                    `;
                    wrap.querySelector('.title').textContent = title;
                    wrap.querySelector('.msg').textContent = message;
                    if (fieldsHtml) {
                        wrap.querySelector('.fields-slot').innerHTML = fieldsHtml;
                    }

                    shadow.appendChild(style);
                    shadow.appendChild(wrap);
                    document.documentElement.appendChild(host);

                    const setHostBase = () => {
                        host.style.setProperty('all', 'initial', 'important');
                        host.style.setProperty('position', 'fixed', 'important');
                        host.style.setProperty('z-index', '2147483647', 'important');
                        host.style.setProperty('pointer-events', 'auto', 'important');
                    };

                    const floatWidthPx = () =>
                        Math.min(480, Math.max(200, window.innerWidth - 24));

                    const applyFullWidth = () => {
                        setHostBase();
                        host.style.setProperty('top', '0', 'important');
                        host.style.setProperty('left', '0', 'important');
                        host.style.setProperty('right', '0', 'important');
                        host.style.setProperty('width', 'auto', 'important');
                    };

                    const applyFloating = (top, left, widthPx) => {
                        setHostBase();
                        host.style.setProperty('top', `${top}px`, 'important');
                        host.style.setProperty('left', `${left}px`, 'important');
                        host.style.setProperty('width', `${widthPx}px`, 'important');
                        host.style.removeProperty('right');
                    };

                    const clampPos = (top, left, widthPx, heightPx) => {
                        const w = widthPx || host.offsetWidth || floatWidthPx();
                        const h = heightPx || host.offsetHeight || 80;
                        const maxLeft = Math.max(0, window.innerWidth - w);
                        const maxTop = Math.max(0, window.innerHeight - h);
                        return {
                            top: Math.min(Math.max(0, top), maxTop),
                            left: Math.min(Math.max(0, left), maxLeft),
                            width: w,
                        };
                    };

                    const savePos = () => {
                        try {
                            sessionStorage.setItem(
                                storageKey,
                                JSON.stringify({
                                    top: host.offsetTop,
                                    left: host.offsetLeft,
                                    width: host.offsetWidth,
                                })
                            );
                        } catch (e) { /* private mode / blocked */ }
                    };

                    let isFloating = false;
                    try {
                        const raw = sessionStorage.getItem(storageKey);
                        if (raw) {
                            const saved = JSON.parse(raw);
                            if (
                                saved &&
                                typeof saved.top === 'number' &&
                                typeof saved.left === 'number'
                            ) {
                                const widthPx =
                                    typeof saved.width === 'number'
                                        ? saved.width
                                        : floatWidthPx();
                                const pos = clampPos(
                                    saved.top,
                                    saved.left,
                                    widthPx,
                                    80
                                );
                                applyFloating(pos.top, pos.left, pos.width);
                                isFloating = true;
                            }
                        }
                    } catch (e) { /* ignore */ }

                    if (!isFloating) {
                        applyFullWidth();
                    }

                    const handle = wrap.querySelector('.drag-handle');
                    let dragging = false;
                    let startX = 0;
                    let startY = 0;
                    let startLeft = 0;
                    let startTop = 0;

                    const onPointerMove = (e) => {
                        if (!dragging) return;
                        if (!isFloating) {
                            isFloating = true;
                            const widthPx = floatWidthPx();
                            const rect = host.getBoundingClientRect();
                            applyFloating(rect.top, rect.left, widthPx);
                            startLeft = host.offsetLeft;
                            startTop = host.offsetTop;
                        }
                        const dx = e.clientX - startX;
                        const dy = e.clientY - startY;
                        const pos = clampPos(
                            startTop + dy,
                            startLeft + dx,
                            host.offsetWidth,
                            host.offsetHeight
                        );
                        applyFloating(pos.top, pos.left, pos.width);
                    };

                    const endDrag = (e) => {
                        if (!dragging) return;
                        dragging = false;
                        try {
                            handle.releasePointerCapture(e.pointerId);
                        } catch (err) { /* already released */ }
                        savePos();
                    };

                    handle.addEventListener('pointerdown', (e) => {
                        if (e.button !== 0) return;
                        dragging = true;
                        handle.setPointerCapture(e.pointerId);
                        startX = e.clientX;
                        startY = e.clientY;
                        if (isFloating) {
                            startLeft = host.offsetLeft;
                            startTop = host.offsetTop;
                        } else {
                            startLeft = 0;
                            startTop = 0;
                        }
                        e.preventDefault();
                    });
                    handle.addEventListener('pointermove', onPointerMove);
                    handle.addEventListener('pointerup', endDrag);
                    handle.addEventListener('pointercancel', endDrag);
                }""",
                {
                    "id": self._OVERLAY_ID,
                    "title": title,
                    "message": message,
                    "color": color,
                    "fieldsHtml": field_list_html,
                    "storageKey": self._OVERLAY_STORAGE_KEY,
                },
            )
        except Exception:
            pass

    def clear_browser_overlay(self) -> None:
        """Remove the in-page banner if it's there."""
        try:
            self.page.evaluate(
                """(id) => {
                    const el = document.getElementById(id);
                    if (el) el.remove();
                }""",
                self._OVERLAY_ID,
            )
        except Exception:
            pass

    def get_attention(self) -> None:
        """Make sure the human notices the request — bring the browser to the
        front, ring the terminal bell, and (on macOS) show a system
        notification.  Best-effort, silently fails on unsupported setups."""
        try:
            self.page.bring_to_front()
        except Exception:
            pass
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass
        try:
            if sys.platform == "darwin":
                import subprocess
                subprocess.Popen(
                    [
                        "osascript", "-e",
                        'display notification "Open the browser to continue." with title "JobCLI: human input required" sound name "Glass"',
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except Exception:
            pass

    @staticmethod
    def _escape_html(s: str) -> str:
        return (
            (s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    # ------------------------------------------------------------------
    # Status / progress helpers
    # ------------------------------------------------------------------

    def show_status(self, message: str, *, phase: Optional[ExecutionPhase] = None) -> None:
        phase_icon = {"rules": "[blue]R[/blue]", "llm": "[magenta]AI[/magenta]", "human": "[yellow]H[/yellow]"}
        prefix = phase_icon.get(phase.value, "[dim]>[/dim]") if phase else "[dim]>[/dim]"
        self.console.print(f"  {prefix} {message}")

    def show_action_plan(self, actions: list[BrowserAction]) -> None:
        """Display what the agent is about to do."""
        if not actions:
            return
        table = Table(title="Agent Action Plan", show_lines=True, expand=False)
        table.add_column("#", style="dim", width=3)
        table.add_column("Action", style="cyan", width=8)
        table.add_column("Target", style="green")
        table.add_column("Value", style="yellow")
        for i, a in enumerate(actions, 1):
            label = a.field_label or a.selector
            val = (a.value or "")[:60]
            table.add_row(str(i), a.action.value.upper(), label[:50], val)
        self.console.print(table)

    def show_phase_banner(self, phase_name: str) -> None:
        self.console.print(Panel(f"[bold]{phase_name}[/bold]", expand=True, border_style="cyan"))

    def show_success(self, message: str) -> None:
        self.console.print(f"  [bold green]OK[/bold green] {message}")

    def show_warning(self, message: str) -> None:
        self.console.print(f"  [bold yellow]!![/bold yellow] {message}")

    def show_error(self, message: str) -> None:
        self.console.print(f"  [bold red]ERR[/bold red] {message}")

    # ------------------------------------------------------------------
    # Checkpoints — behaviour depends on InteractionMode
    # ------------------------------------------------------------------

    def approve_action_plan(self, actions: list[BrowserAction]) -> bool:
        """MANUAL mode: show plan and wait for approval.  Others: auto-approve."""
        if self.mode == InteractionMode.MANUAL:
            self.show_action_plan(actions)
            return Confirm.ask("  Execute these actions?", default=True)
        if self.mode == InteractionMode.SUPERVISED:
            self.show_action_plan(actions)
        return True

    def confirm_submission(self) -> bool:
        """Before clicking Submit — always confirm in SUPERVISED/MANUAL, skip in AUTO."""
        if self.mode == InteractionMode.AUTO:
            return True
        self.console.print(
            "\n  [bold yellow]Ready to submit application.[/bold yellow]"
        )
        self.show_browser_overlay(
            title="Ready to submit your application",
            message="Confirm in the terminal to send. Hit cancel there to abort.",
            kind="info",
        )
        self.get_attention()
        try:
            # Reuse ask_yes_no so y/yes/Y, Enter (default yes), and whitespace
            # all behave consistently — bare startswith("y") rejected " y".
            return self.ask_yes_no("  Submit now?", default=True)
        finally:
            self.clear_browser_overlay()

    def pause_for_review(self, message: str, *, timeout_seconds: int = 0) -> None:
        """General-purpose pause. AUTO skips; SUPERVISED auto-continues after timeout;
        MANUAL always waits for Enter."""
        if self.mode == InteractionMode.AUTO:
            return
        self.console.print(f"\n  [dim]{message}[/dim]")
        if self.mode == InteractionMode.MANUAL or timeout_seconds == 0:
            self._get_user_input("  Press ENTER to continue...")
            return
        # SUPERVISED: auto-continue after timeout, but allow early Enter
        self.console.print(f"  [dim]Auto-continuing in {timeout_seconds}s (press ENTER to skip wait)...[/dim]")
        self._wait_with_early_exit(timeout_seconds)

    def handoff_to_human(
        self,
        reason: str,
        *,
        hint: Optional[str] = None,
        wait_for_navigation_seconds: int = 8,
        force_block: bool = False,
        submission_checker: Optional[Callable[[], tuple[bool, bool, dict]]] = None,
    ) -> HandoffResult:
        """Hand full browser control to the human and **resume from wherever
        they leave it**.

        This is the canonical "agent is stuck — you take over" checkpoint.
        It:

        1. Snapshots the current URL so we can detect whether the human
           advanced the page.
        2. Shows a prominent modal explaining the situation, what the human
           should do, and how to hand back ("press ENTER when done").
        3. Blocks until the human presses ENTER (or types ``cancel``).
        4. After ENTER, gives the page a moment to finish any in-flight
           navigation the human triggered (e.g. clicked Next), then snapshots
           the new URL/title.
        5. Returns a :class:`HandoffResult` that the engine uses to resume
           **from the human's current page**, *not* from where the agent got
           stuck.

        In ``AUTO`` mode the default behaviour is to print a "cannot block
        indefinitely" warning and use a short 60s timeout — appropriate for
        unattended batches where blocking forever would deadlock CI.

        Pass ``force_block=True`` for checkpoints that MUST get a human
        decision regardless of mode (e.g. the compulsory pre-submit review).
        When ``force_block=True`` the AUTO short-circuit is bypassed and the
        wait uses the full 600s timeout, matching SUPERVISED/MANUAL. An
        environment-variable escape hatch ``WBOX_BYPASS_PRE_SUBMIT_REVIEW=1``
        restores the AUTO short-circuit even for forced calls — intended for
        CI/headless smoke tests only.
        """
        url_before = ""
        try:
            url_before = self.page.url or ""
        except Exception:
            pass

        bypass_force = (
            force_block
            and os.environ.get("WBOX_BYPASS_PRE_SUBMIT_REVIEW", "0") == "1"
        )
        effective_force = force_block and not bypass_force

        if (
            self.mode == InteractionMode.AUTO
            and not self.is_server
            and not effective_force
        ):
            self.show_error(
                f"Agent stuck ({reason}) but running in AUTO mode — cannot block indefinitely."
            )
            wait_timeout = 60
        else:
            wait_timeout = 600

        if self.mode == InteractionMode.AUTO and self.is_server:
             self.show_warning(
                f"Agent is stuck ({reason}). Pausing for manual intervention (Dashboard mode)."
             )

        body_lines: list[str] = []
        body_lines.append(f"[bold]Reason:[/bold] {reason}")
        if hint:
            body_lines.append(f"[bold]Suggested action:[/bold] {hint}")
        body_lines.append("")
        body_lines.append(
            "[bold]>> The browser is yours.[/bold]  Do whatever you need: click, "
            "type, navigate, fill missing fields, advance to the next step, etc."
        )
        body_lines.append(
            "When you're finished, press [bold green]ENTER[/bold green] here and the "
            "agent will continue from [bold]whichever page you ended up on[/bold] "
            "(it will NOT go back to where it got stuck)."
        )
        body_lines.append(
            "Type [bold red]cancel[/bold red] + ENTER to abort this application, "
            "[bold cyan]skip[/bold cyan] to move to the next job, or "
            "[bold magenta]q[/bold magenta] / [bold magenta]quit[/bold magenta] "
            "(or Ctrl+C) to exit JobCLI entirely."
        )

        self.console.print(
            Panel(
                "\n".join(body_lines),
                title="[bold yellow]>>> AGENT HANDED OFF — YOU ARE IN CONTROL <<<[/bold yellow]",
                border_style="yellow",
                expand=True,
            )
        )

        # Browser-side cue so the user notices while looking at the form.
        self.show_browser_overlay(
            title="JobCLI handed control back to you",
            message=reason + (f" — {hint}" if hint else ""),
            kind="warning",
        )
        self.get_attention()

        response = self._get_user_input(
            f"  Press ENTER when done, or type skip / cancel + ENTER "
            f"[{wait_timeout}s timeout]: ",
            timeout_seconds=wait_timeout,
            poll_browser_fields=True,
            submission_checker=submission_checker,
        )

        if response == SUBMITTED_SENTINEL:
            self.clear_browser_overlay()
            url_after = url_before
            title_after = ""
            try:
                url_after = self.page.url or url_before
            except Exception:
                pass
            try:
                title_after = self.page.title() or ""
            except Exception:
                pass
            if self.logger is not None:
                try:
                    self.logger.info(
                        "human_submitted_detected_live",
                        phase=ExecutionPhase.HUMAN,
                        url_after=url_after,
                    )
                except Exception:
                    pass
            return HandoffResult(
                page=self.page,
                url_before=url_before,
                url_after=url_after,
                title_after=title_after,
                advanced=True,
                cancelled=False,
                submitted=True,
            )

        if response is None:
            self.console.print(
                f"\n  [bold yellow] No response for {wait_timeout} seconds — skipping this job.[/bold yellow]"
            )
            self.clear_browser_overlay()
            return HandoffResult(
                page=self.page,
                url_before=url_before,
                url_after=url_before,
                title_after="",
                advanced=False,
                cancelled=True,
            )

        response = response.strip().lower()

        # Clear the in-page banner now that the human has handed control back.
        self.clear_browser_overlay()

        if response == "cancel":
            return HandoffResult(
                page=self.page,
                url_before=url_before,
                url_after=url_before,
                title_after="",
                advanced=False,
                cancelled=True,
            )

        if response in ("skip", "skipp", "skp", "s"):
            self.console.print(
                "\n  [bold yellow]Skipped this job — opening the next URL.[/bold yellow]"
            )
            return HandoffResult(
                page=self.page,
                url_before=url_before,
                url_after=url_before,
                title_after="",
                advanced=False,
                cancelled=False,
                skipped=True,
            )

        # Give any in-flight navigation a chance to settle.  If the human just
        # clicked Next/Continue right before pressing ENTER, the page may still
        # be loading.
        try:
            self.page.wait_for_load_state(
                "domcontentloaded", timeout=wait_for_navigation_seconds * 1000
            )
        except Exception:
            pass
        try:
            self.page.wait_for_load_state(
                "networkidle", timeout=max(2, wait_for_navigation_seconds // 2) * 1000
            )
        except Exception:
            pass

        url_after = url_before
        title_after = ""
        try:
            url_after = self.page.url or url_before
        except Exception:
            pass
        try:
            title_after = self.page.title() or ""
        except Exception:
            pass

        advanced = urls_meaningfully_different(url_before, url_after)

        if advanced:
            self.show_success(
                f"Resuming from your current page: {title_after or url_after}"
            )
        else:
            self.show_status("Resuming on the same page.", phase=ExecutionPhase.HUMAN)

        if self.logger is not None:
            try:
                self.logger.info(
                    "human_handoff_resumed",
                    phase=ExecutionPhase.HUMAN,
                    url_before=url_before,
                    url_after=url_after,
                    advanced=advanced,
                    reason=reason,
                )
            except Exception:
                pass

        return HandoffResult(
            page=self.page,
            url_before=url_before,
            url_after=url_after,
            title_after=title_after,
            advanced=advanced,
            cancelled=False,
        )

    def request_field_help(
        self,
        label: str,
        value: str,
        options: Optional[list[str]] = None,
    ) -> Optional[str]:
        """Ask the human for help with a specific field that the agent failed to fill."""
        self.get_attention()
        self.console.print(Panel(
            f"I couldn't find the field for: [bold cyan]{label}[/bold cyan]\n"
            f"Expected value: [bold green]{value}[/bold green]",
            title="[bold yellow]FIELD SELECTOR FAILED[/bold yellow]",
            border_style="yellow",
        ))
        
        choices = ["(B) I'll fill it in browser", "(V) Enter new value in terminal"]
        if options:
            choices.append("(O) Select from options")
        
        self.console.print("  What should I do?")
        for c in choices:
            self.console.print(f"    {c}")
            
        try:
            raw_res = self._get_user_input("  Choice [B]: ", default="b")
            res = (raw_res or "b").lower()
            if res.startswith("v"):
                new_val = self._get_user_input(f"  Value for '{label}': ", default=value)
                return new_val
            if res.startswith("o") and options:
                # Show options with numbers
                for i, opt in enumerate(options, 1):
                    self.console.print(f"    [{i}] {opt}")
                opt_idx = self._get_user_input(f"  Select option (1-{len(options)}): ")
                if not opt_idx:
                    return None
                try:
                    return options[int(opt_idx) - 1]
                except (ValueError, IndexError):
                    return None
            return None
        except Exception:
            return None

    def handle_captcha(self) -> bool:
        """CAPTCHA detected — always pause for human except in AUTO (which gives up)."""
        if self.mode == InteractionMode.AUTO:
            self.show_error("CAPTCHA detected in auto mode — cannot proceed.")
            return False
        self.show_browser_overlay(
            title="CAPTCHA detected — please solve it in the browser",
            message="JobCLI cannot solve CAPTCHAs. Solve it on this page, then return to the terminal and press ENTER.",
            kind="error",
        )
        self.get_attention()
        self.console.print(
            "\n  [bold red]CAPTCHA or verification detected.[/bold red]"
        )
        self.console.print("  Please solve it in the browser window.")
        res = self._get_user_input("  Press ENTER when done: ")
        if res == "cancel":
            self.clear_browser_overlay()
            return False
        self.clear_browser_overlay()
        return True

    def request_field_input(
        self,
        field_label: str,
        *,
        options: Optional[list[str]] = None,
        current_value: Optional[str] = None,
        question_text: Optional[str] = None,
        required: bool = True,
        prompt_optional: bool = False,
    ) -> Optional[str]:
        """Ask the human for a missing/failed field value.

        Behaviour:
          1. If the field already has a value in the browser, return None.
          1a. If the same question was already answered in THIS session
              (e.g. earlier page of a multi-step form), reuse it silently.
          2. If a value is already in the DB (any source), return it silently
             — the agent never re-asks a question it has answered before.
          3. Optional fields are only prompted when ``prompt_optional=True``
             (the failed-fields review pass). LLM ``ASK`` actions never set
             that flag, so optional questions are skipped in the terminal.
          4. AUTO mode skips human prompts entirely (returns None).
          5. SUPERVISED / MANUAL show a modal-style panel and BLOCK until the
             human responds.  The answer is auto-saved to the DB.
          6. Typing ``skip`` (or pressing Enter on optional fields) leaves the
             field blank — it is never written into the form.
        """
        from jobcli.utils.exit_signal import is_skip_field_keyword

        clean_label = self._strip_prompt_markup(field_label)

        if current_value and current_value.strip():
            return None

        if not required and not prompt_optional:
            return None

        # 0. Browser-first: user may have filled the field while the agent waited.
        browser_val = self._read_browser_field_value(clean_label)
        if browser_val:
            self.console.print(
                f"  [dim]Using value already entered in browser for "
                f"[cyan]{clean_label}[/cyan]: '{browser_val}'[/dim]"
            )
            if self.logger:
                self.logger.info(
                    f"Browser already has value for '{clean_label}'.",
                    phase=ExecutionPhase.HUMAN,
                )
            return browser_val

        # 0a. Session cache: same question asked earlier in THIS application run?
        #     This fires on repeated pages of multi-step forms where the ATS
        #     re-renders the same field (e.g. phone, LinkedIn) on every step.
        session_key = clean_label.lower().strip()
        session_cached = self._session_question_cache.get(session_key)
        if session_cached:
            self.console.print(
                f"  [dim]Reusing session answer for [cyan]{clean_label}[/cyan]: '{session_cached}'[/dim]"
            )
            if self.logger:
                self.logger.info(
                    f"Session cache hit for field '{clean_label}'.",
                    phase=ExecutionPhase.HUMAN,
                )
            from jobcli.utils.form_sync import apply_field_value
            if self.page and apply_field_value(self.page, clean_label, session_cached, options):
                self.show_success(f"Filled '{clean_label}' from session cache")
            return session_cached

        # 1. DB-first: did we already answer this on a previous job?
        cached, source = self.lookup_db_answer(clean_label)
        if cached:
            self.console.print(
                f"  [dim]Reusing answer for [cyan]{clean_label}[/cyan] from {source}: '{cached}'[/dim]"
            )
            if self.logger:
                self.logger.info(
                    f"DB hit for field '{clean_label}' (source={source}).",
                    phase=ExecutionPhase.HUMAN,
                )
            # Also store in session cache so subsequent pages get it too
            self._session_question_cache[session_key] = cached
            from jobcli.utils.form_sync import apply_field_value

            if self.page and apply_field_value(self.page, clean_label, cached, options):
                self.show_success(f"Filled '{clean_label}' from memory")
            return cached

        if self.mode == InteractionMode.AUTO and not self.is_server:
            return None

        # 2. Show a clear modal-style block — the agent has paused.
        question = question_text or f"Please provide a value for: {clean_label}"
        body_lines = [
            f"[bold]Question:[/bold] {question}",
            f"[dim]Field label:[/dim] [cyan]{clean_label}[/cyan]",
        ]
        if required:
            body_lines.append("[red]*required[/red]")
        else:
            body_lines.append("[dim](optional — Enter or type skip to leave blank)[/dim]")
        if options:
            body_lines.append("")
            body_lines.append("[dim]Available options:[/dim]")
            for i, opt in enumerate(options, 1):
                body_lines.append(f"  {i}. {opt}")
        body_lines.append("")
        body_lines.append(
            "[dim]You can answer here OR fill the field in the browser, then press Enter. "
            "Type [bold]skip[/bold] to leave this field blank. "
            "Type [bold]q[/bold] / [bold]quit[/bold] (or Ctrl+C) to exit JobCLI.[/dim]"
        )
        self.console.print(
            Panel(
                "\n".join(body_lines),
                title="[bold yellow]>>> AGENT PAUSED — INPUT REQUIRED <<<[/bold yellow]",
                border_style="yellow",
                expand=True,
            )
        )

        # Browser-side cue + attention-grabber so the user notices.
        self.show_browser_overlay(
            title=f"JobCLI needs your input: {field_label}",
            message=question,
            kind="warning",
            fields=[field_label] + (options[:5] if options else []),
        )
        self.get_attention()

        answer = self._get_user_input(
            "  Your answer: ",
            poll_browser_fields=True,
        )
        answer = (answer or "").strip()
        self.clear_browser_overlay()

        # User may have filled the field in the browser instead of the terminal.
        browser_val = self._read_browser_field_value(clean_label)
        if browser_val:
            self.console.print(
                f"  [dim]Using browser value for [cyan]{clean_label}[/cyan]: '{browser_val}'[/dim]"
            )
            self.persist_human_answer(clean_label, browser_val)
            # Store in session cache so repeated pages don't ask again
            self._session_question_cache[session_key] = browser_val
            return browser_val

        if not answer or is_skip_field_keyword(answer):
            if answer and is_skip_field_keyword(answer):
                self.console.print(
                    f"  [dim]Skipping [cyan]{clean_label}[/cyan] (left blank).[/dim]"
                )
            return None

        # 3. Persist and apply to the live form immediately
        self.persist_human_answer(clean_label, answer)
        # Store in session cache so repeated questions on later pages are auto-filled
        self._session_question_cache[session_key] = answer
        from jobcli.utils.form_sync import apply_field_value

        if self.page and apply_field_value(self.page, clean_label, answer, options):
            self.show_success(f"Filled '{clean_label}' in browser")
        else:
            self.show_warning(
                f"Saved answer for '{clean_label}' to memory but could not "
                "auto-fill in browser — check the field manually."
            )
        self.console.print(
            f"  [green]+[/green] Saved to memory: [cyan]{clean_label}[/cyan] = '{answer}'"
        )
        return answer

    def request_help_finding_element(
        self,
        task: str,
        ats_type: ATSType = ATSType.UNKNOWN,
    ) -> tuple[bool, Optional[str], Optional[SelectorType]]:
        """Deprecated — always returns ``(False, None, None)``.

        The old implementation rendered a terminal-only "Select from
        detected elements / Provide manual selector / Skip" picker.  It
        was confusing (users had to translate the agent's DOM list into
        CSS selectors themselves) and it blocked the far cleaner path of
        handing the actual browser back to the human.  Callers are
        expected to fall through to ``handoff_to_human`` on False, which
        they all do.
        """
        if self.logger:
            self.logger.debug(
                "request_help_finding_element is deprecated — "
                "routing directly to handoff_to_human.",
                phase=ExecutionPhase.HUMAN,
                task=task,
            )
        return False, None, None

    def ask_continue(self) -> bool:
        """Should the agent continue with this application?  AUTO always says yes."""
        if self.mode == InteractionMode.AUTO:
            return True
        return self.ask_yes_no("  Continue with this application?")

    def ask_yes_no(self, question: str, default: bool = True) -> bool:
        """Ask a yes/no question and return the result.

        Uses the unified ``_get_user_input`` pipeline so the universal quit
        keywords (``q``, ``quit``, ``exit``, ``:q``) and Ctrl+C work here
        too — previously this used ``rich.prompt.Confirm.ask`` directly,
        which only accepted y/n/yes/no and turned Ctrl+C into a stack
        trace. ExitRequested propagates to the apply loop for graceful
        shutdown.
        """
        self.get_attention()

        # If in AUTO mode, we can't block, so return default
        if self.mode == InteractionMode.AUTO:
            return default

        # Notify dashboard explicitly
        try:
            if self.logger:
                self.logger.emit_event({
                    "type": "human_input_required",
                    "input_type": "confirm",
                    "prompt": question,
                    "default": default
                })
        except Exception:
            pass

        default_hint = "Y/n" if default else "y/N"
        # ``_get_user_input`` raises ExitRequested for quit keywords / Ctrl+C
        # which the apply loop catches — we don't need to handle it here.
        prompt = f"  {question} [{default_hint}] (q to quit): "
        answer = self._get_user_input(prompt, default="")
        if answer is None or not answer.strip():
            return default
        a = answer.strip().lower()
        if a in ("y", "yes", "yep", "yeah", "true", "1"):
            return True
        if a in ("n", "no", "nope", "false", "0"):
            return False
        # Anything else (including weird non-yes/no text) → fall back to
        # default rather than asking again, matching prior rich.Confirm
        # behavior. Quit keywords are intercepted earlier in
        # ``_get_user_input``.
        return default

    def show_failed_fields(
        self,
        failed_actions: list[BrowserAction],
        *,
        dropdown_options_by_selector: Optional[dict[str, list[str]]] = None,
    ) -> list[BrowserAction]:
        """For each failed field: try DB first, then prompt human.

        Splits failed fields into two tiers:

        * **Required** — the field's ``required`` flag is true (propagated
          from the AX tree). Prompted first, one by one, with a red
          ``*required`` tag so the user can't miss them.
        * **Optional** — everything else. Shown afterwards in a dim section
          with ``(optional, press Enter to skip)``; the user can skip any
          of them by pressing Enter.

        Returns a list of new :class:`BrowserAction` objects with the
        collected values populated, ready to be re-executed against the
        browser. Every human-supplied answer is also saved to the DB (via
        ``request_field_input`` → ``AgentMemory.save_field_answer``) so
        the next job on this ATS reuses it automatically.
        """
        if not failed_actions:
            return []

        actionable = [
            a for a in failed_actions
            if a.action in (ActionType.SELECT, ActionType.FILL, ActionType.TYPE)
            and not (a.value and a.value.strip())
        ]
        if not actionable:
            return []

        # Split into required vs optional. We rely on the AX-tree-driven
        # ``required`` flag that the LLM client propagates onto each
        # action; anything unmarked is treated as optional.
        required = [a for a in actionable if a.required]
        optional = [a for a in actionable if not a.required]

        # Header banner — clear "agent stopped" signal with counts so the
        # user sees the scope at a glance.
        if self.mode != InteractionMode.AUTO:
            self.console.print(
                Panel(
                    (
                        f"[bold]Required fields:[/bold] [yellow]{len(required)}[/yellow]    "
                        f"[dim]Optional fields:[/dim] [dim]{len(optional)}[/dim]\n"
                        "[dim]Required fields must be answered. Optional fields can be "
                        "skipped by pressing Enter. Every answer you give is saved and "
                        "reused on future applications.[/dim]"
                    ),
                    title="[bold yellow]>>> AGENT PAUSED — REVIEW NEEDED <<<[/bold yellow]",
                    border_style="yellow",
                )
            )

        filled: list[BrowserAction] = []

        def _collect(act: BrowserAction, label_suffix: str) -> None:
            label = act.field_label or act.selector
            options = (dropdown_options_by_selector or {}).get(act.selector)
            answer = self.request_field_input(
                f"{label}{label_suffix}",
                options=options,
                required=act.required,
                prompt_optional=not act.required,
            )
            if not answer:
                return
            # Coerce FILL → SELECT when the field is a known dropdown so the
            # executor uses the dropdown-friendly strategy.
            action_type = act.action
            if options and action_type in (ActionType.FILL, ActionType.TYPE):
                action_type = ActionType.SELECT
            filled.append(
                act.model_copy(update={"value": answer, "action": action_type})
            )

        if required:
            self.console.print(
                f"\n  [bold yellow]Required fields[/bold yellow] "
                f"[dim](must answer, {len(required)} total)[/dim]"
            )
            for act in required:
                _collect(act, "  [red]*required[/red]")

        if optional and self.mode != InteractionMode.AUTO:
            self.console.print(
                f"\n  [dim]Optional fields ({len(optional)} total — press Enter to skip)[/dim]"
            )
            for act in optional:
                _collect(act, "  [dim](optional, Enter to skip)[/dim]")

        return filled

    def pause_for_form_review(self) -> None:
        """Pause after auto-fill so the human can review/fix the form before submit.

        Flow
        ----
        1. Prints a cyan "REVIEW REQUIRED" banner explaining what to do.
        2. Waits indefinitely for ENTER (no timeout — human must confirm explicitly).
        3. Once ENTER is pressed, shows a 2-second submit countdown.
           The human can press ENTER again to skip the countdown and submit immediately.

        In AUTO mode the pause is skipped entirely and submit fires straight away.
        """
        if self.mode == InteractionMode.AUTO:
            return

        self.console.print(
            Panel(
                "[bold]Form has been pre-filled automatically.[/bold]\n\n"
                "  [cyan]→[/cyan] Check all fields in the browser\n"
                "  [cyan]→[/cyan] Fix anything that looks wrong or is left blank\n"
                "  [cyan]→[/cyan] When everything looks good, press "
                "[bold green]ENTER[/bold green] to start the submit countdown",
                title="[bold cyan]>>> REVIEW REQUIRED — CHECK BROWSER <<<[/bold cyan]",
                border_style="cyan",
            )
        )
        try:
            # No timeout — wait until the human explicitly presses ENTER.
            self._get_user_input(
                "  [cyan]Press ENTER when ready to submit →[/cyan] ",
                timeout_seconds=None,
                default="",
            )
        except Exception:
            pass

        # 2-second countdown so the human has one last chance to abort (Ctrl+C).
        self.console.print(
            "\n  [dim]Submitting in [bold]2s[/bold]… "
            "(press [bold green]ENTER[/bold green] to submit now)[/dim]"
        )
        try:
            self._get_user_input("", timeout_seconds=2, default="")
        except Exception:
            pass

    def final_browser_pause(self) -> None:
        """Keep the browser open briefly, then auto-advance to the next job.

        Shows a 2-second countdown in the terminal. The user can press ENTER
        to advance immediately, or simply wait — the agent moves on
        automatically after 2 seconds without any keypress required.
        """
        if self.mode == InteractionMode.AUTO:
            return
        self.console.print(
            "\n  [dim]Moving to next job in [bold]2s[/bold]… "
            "(press [bold green]ENTER[/bold green] to go now)[/dim]"
        )
        try:
            self._get_user_input("", timeout_seconds=2, default="")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Internal helpers (unchanged from old HumanInterface, but private)
    # ------------------------------------------------------------------

    def _wait_with_early_exit(self, seconds: int) -> None:
        """Wait for up to *seconds* but return early if the user presses Enter."""
        try:
            import select
            rlist, _, _ = select.select([sys.stdin], [], [], float(seconds))
            if rlist:
                sys.stdin.readline()
        except Exception:
            time.sleep(seconds)

    def _save_learned_locator(
        self, purpose: str, ats_type: ATSType, selector: str,
        selector_type: SelectorType, notes: str,
    ) -> None:
        try:
            locator = LearnedLocator(
                ats_type=ats_type, selector=selector, selector_type=selector_type,
                purpose=purpose, notes=notes, created_by="human",
            )
            self.locator_repo.create(locator)
            self.show_success("Locator saved")
            if self.logger:
                self.logger.info("Learned locator saved", phase=ExecutionPhase.HUMAN, purpose=purpose, selector=selector)
        except Exception as e:
            self.show_error(f"Failed to save locator: {e}")
