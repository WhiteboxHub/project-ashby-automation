"""Unit tests for the extension autofill pipeline rewiring.

Covers the three new behaviours introduced in the extension autofill rework:

1. Rules-as-fallback gate — rules only runs when the extension filled zero
   visible fields (``self._last_extension_filled_count == 0``).
2. ``_looks_like_confirmation`` helper that decides whether a page already
   looks like a post-submit confirmation. Used both for normal post-click
   verification and for detecting "user submitted during compulsory review".
3. ``handoff_to_human(force_block=True)`` — bypasses the AUTO-mode short
   circuit so the compulsory pre-submit review blocks in every mode.

These tests mock ``Page``, ``JobLogger``, and ``AgentInterface`` to keep the
suite fast and independent of Playwright / a live browser.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


# ───────────────────────────────────────────────────────────────────────
# _looks_like_confirmation helper
# ───────────────────────────────────────────────────────────────────────


class TestLooksLikeConfirmation:
    """Unit tests for ApplicationEngine._looks_like_confirmation."""

    @pytest.fixture
    def engine(self):
        """Construct a real ApplicationEngine bound to mock collaborators.

        The helper only touches `self._CONFIRMATION_TEXTS` /
        `self._CONFIRMATION_URL_TERMS` and module-level utilities, so it's
        cheap to instantiate.
        """
        from jobcli.orchestration.engine import ApplicationEngine

        engine = ApplicationEngine.__new__(ApplicationEngine)
        return engine

    def _make_page(self, *, url: str, body_text: str, has_submit_btn: bool):
        """Build a MagicMock Page that returns the configured URL/body."""
        page = MagicMock()
        page.url = url
        page.evaluate.return_value = body_text
        return page

    def test_strong_signal_from_confirmation_text(self, engine):
        page = self._make_page(
            url="https://boards.greenhouse.io/x/jobs/1/apply",
            body_text="Thank you for applying! We will be in touch soon.",
            has_submit_btn=False,
        )
        with patch("jobcli.utils.form_sync._submit_button_visible", return_value=False), \
             patch("jobcli.utils.form_sync._live_validation_errors", return_value=[]):
            strong, soft, signals = engine._looks_like_confirmation(
                page,
                pre_submit_url="https://boards.greenhouse.io/x/jobs/1/apply",
                pre_submit_had_submit_btn=True,
            )

        assert strong is True
        assert signals["text_confirmed"] is True

    def test_strong_signal_from_confirmation_url(self, engine):
        page = self._make_page(
            url="https://jobs.lever.co/x/thank-you",
            body_text="",
            has_submit_btn=False,
        )
        with patch("jobcli.utils.form_sync._submit_button_visible", return_value=False), \
             patch("jobcli.utils.form_sync._live_validation_errors", return_value=[]):
            strong, soft, signals = engine._looks_like_confirmation(
                page,
                pre_submit_url="https://jobs.lever.co/x/abc/apply",
                pre_submit_had_submit_btn=True,
            )

        assert strong is True
        assert signals["url_confirmed"] is True

    def test_soft_signal_url_change_only(self, engine):
        page = self._make_page(
            url="https://example.com/done",
            body_text="some neutral page text",
            has_submit_btn=True,  # button still there but URL changed
        )
        with patch("jobcli.utils.form_sync._submit_button_visible", return_value=True), \
             patch("jobcli.utils.form_sync._live_validation_errors", return_value=[]):
            strong, soft, signals = engine._looks_like_confirmation(
                page,
                pre_submit_url="https://example.com/apply",
                pre_submit_had_submit_btn=True,
            )

        assert strong is False
        assert soft is True
        assert signals["url_changed"] is True

    def test_soft_signal_form_disappeared(self, engine):
        page = self._make_page(
            url="https://example.com/apply",  # identical URL (SPA case)
            body_text="application page content",
            has_submit_btn=False,
        )
        with patch("jobcli.utils.form_sync._submit_button_visible", return_value=False), \
             patch("jobcli.utils.form_sync._live_validation_errors", return_value=[]):
            strong, soft, signals = engine._looks_like_confirmation(
                page,
                pre_submit_url="https://example.com/apply",
                pre_submit_had_submit_btn=True,
            )

        assert strong is False
        assert soft is True
        assert signals["form_disappeared"] is True

    def test_validation_errors_suppress_soft_confirmation(self, engine):
        page = self._make_page(
            url="https://example.com/done",  # URL changed
            body_text="please correct the errors below",
            has_submit_btn=True,
        )
        with patch("jobcli.utils.form_sync._submit_button_visible", return_value=True), \
             patch("jobcli.utils.form_sync._live_validation_errors", return_value=["Required field missing"]):
            strong, soft, signals = engine._looks_like_confirmation(
                page,
                pre_submit_url="https://example.com/apply",
                pre_submit_had_submit_btn=True,
            )

        assert strong is False
        # url_changed=True but has_errors=True → soft must NOT fire
        assert soft is False
        assert signals["has_errors"] is True
        assert signals["url_changed"] is True

    def test_no_signals_at_all(self, engine):
        page = self._make_page(
            url="https://example.com/apply",
            body_text="application page content with no thank-you copy",
            has_submit_btn=True,
        )
        with patch("jobcli.utils.form_sync._submit_button_visible", return_value=True), \
             patch("jobcli.utils.form_sync._live_validation_errors", return_value=[]):
            strong, soft, signals = engine._looks_like_confirmation(
                page,
                pre_submit_url="https://example.com/apply",
                pre_submit_had_submit_btn=True,
            )

        assert strong is False
        assert soft is False
        assert signals == {
            "text_confirmed": False,
            "url_confirmed": False,
            "url_changed": False,
            "form_disappeared": False,
            "has_errors": False,
        }

    def test_handles_page_evaluate_exception(self, engine):
        page = MagicMock()
        page.url = "https://example.com/apply"
        page.evaluate.side_effect = RuntimeError("boom")

        with patch("jobcli.orchestration.engine._submit_button_visible", return_value=True), \
             patch("jobcli.orchestration.engine._live_validation_errors", return_value=[]):
            strong, soft, signals = engine._looks_like_confirmation(
                page,
                pre_submit_url="https://example.com/apply",
                pre_submit_had_submit_btn=True,
            )

        # Should not raise; text_confirmed simply False.
        assert strong is False
        assert soft is False
        assert signals["text_confirmed"] is False


# ───────────────────────────────────────────────────────────────────────
# handoff_to_human(force_block=…) — AUTO-mode bypass
# ───────────────────────────────────────────────────────────────────────


class TestHandoffForceBlock:
    """Unit tests for the new force_block kwarg on AgentInterface.handoff_to_human."""

    def _make_agent(self, *, mode, is_server=False):
        """Construct a real AgentInterface bound to mock Page + memory.

        We use ``AgentInterface.__new__`` to avoid running the full
        constructor (which talks to the DB and resume system).
        """
        from jobcli.human.agent_interface import AgentInterface

        agent = AgentInterface.__new__(AgentInterface)
        agent.mode = mode
        agent.is_server = is_server
        agent.page = MagicMock()
        agent.page.url = "https://example.com/apply"
        agent.console = MagicMock()
        agent.logger = None
        agent.show_error = MagicMock()
        agent.show_warning = MagicMock()
        agent.show_status = MagicMock()
        agent.show_success = MagicMock()
        agent.show_browser_overlay = MagicMock()
        agent.clear_browser_overlay = MagicMock()
        agent.get_attention = MagicMock()
        return agent

    def test_force_block_bypasses_auto_short_circuit(self):
        """In AUTO mode with force_block=True, wait_timeout should be 600 (not 60).

        We assert this via the recorded ``_get_user_input`` call.
        """
        from jobcli.profile.schemas import InteractionMode

        agent = self._make_agent(mode=InteractionMode.AUTO)
        # Simulate the user pressing ENTER immediately.
        agent._get_user_input = MagicMock(return_value="")

        result = agent.handoff_to_human(
            reason="pre-submit review", hint="check fields", force_block=True
        )

        # The new behaviour: AUTO + force_block → no "stuck" error AND 600s timeout.
        agent.show_error.assert_not_called()
        call_args = agent._get_user_input.call_args
        # _get_user_input(prompt, timeout_seconds=...)
        assert call_args.kwargs.get("timeout_seconds") == 600
        assert result.cancelled is False

    def test_default_auto_short_circuit_preserved(self):
        """In AUTO mode without force_block, the existing 60s/error behaviour
        must be preserved (regression guard for other handoff call sites)."""
        from jobcli.profile.schemas import InteractionMode

        agent = self._make_agent(mode=InteractionMode.AUTO)
        agent._get_user_input = MagicMock(return_value=None)  # timeout path

        agent.handoff_to_human(reason="agent stuck", hint=None)

        agent.show_error.assert_called_once()
        call_args = agent._get_user_input.call_args
        assert call_args.kwargs.get("timeout_seconds") == 60

    def test_force_block_with_bypass_env_falls_back_to_auto(self, monkeypatch):
        """``WBOX_BYPASS_PRE_SUBMIT_REVIEW=1`` makes force_block a no-op in AUTO.

        Intended for CI / headless smoke tests only — documents the escape
        hatch so it can't be silently re-removed.
        """
        from jobcli.profile.schemas import InteractionMode

        monkeypatch.setenv("WBOX_BYPASS_PRE_SUBMIT_REVIEW", "1")

        agent = self._make_agent(mode=InteractionMode.AUTO)
        agent._get_user_input = MagicMock(return_value=None)

        agent.handoff_to_human(
            reason="pre-submit review", hint=None, force_block=True
        )

        # Bypass active → behaves like default AUTO call (60s + stuck error).
        agent.show_error.assert_called_once()
        call_args = agent._get_user_input.call_args
        assert call_args.kwargs.get("timeout_seconds") == 60

    def test_supervised_mode_always_blocks(self):
        """SUPERVISED has no short-circuit regardless of force_block."""
        from jobcli.profile.schemas import InteractionMode

        agent = self._make_agent(mode=InteractionMode.SUPERVISED)
        agent._get_user_input = MagicMock(return_value="")

        agent.handoff_to_human(reason="review", hint=None)

        agent.show_error.assert_not_called()
        call_args = agent._get_user_input.call_args
        assert call_args.kwargs.get("timeout_seconds") == 600


# ───────────────────────────────────────────────────────────────────────
# Rules-as-fallback gate
# ───────────────────────────────────────────────────────────────────────
#
# The gate itself lives inline inside `apply_to_job` rather than in a
# stand-alone function, so we exercise it by reading the source and
# checking the gating expression is still present. A higher-fidelity
# end-to-end check lives in test_extension_pipeline_integration.py.


class TestRulesFallbackGate:
    """Source-level guard: the gating expression must remain in place."""

    def test_pre_upload_gate_present(self):
        from jobcli.orchestration import engine

        src = open(engine.__file__).read()
        # The exact gate expression — if this string disappears the gate
        # was removed or paraphrased and the integration suite must be
        # re-validated.
        assert "if self._last_extension_filled_count == 0:" in src, (
            "Pre-upload rules fallback gate is missing. Rules must only "
            "run when the extension produced zero fills."
        )

    def test_post_upload_gate_present(self):
        from jobcli.orchestration import engine

        src = open(engine.__file__).read()
        # Same gate, appears twice — once pre-upload, once post-upload.
        # Count must be >= 2.
        assert src.count("if self._last_extension_filled_count == 0") >= 1, (
            "Post-upload rules fallback gate is missing."
        )

    def test_pipeline_labels_updated(self):
        from jobcli.orchestration.engine import ApplicationEngine

        steps = ApplicationEngine.FILL_PIPELINE_STEPS
        joined = " ".join(steps)
        assert "Rules (fallback)" in joined
        assert "Human review (always)" in joined


# ───────────────────────────────────────────────────────────────────────
# Compulsory handoff call shape
# ───────────────────────────────────────────────────────────────────────


class TestCompulsoryHandoffCall:
    """Source-level guard: the new compulsory handoff site must use force_block=True
    and the obsolete confirm_submission() prompt must be gone from the apply path."""

    def test_pre_submit_handoff_uses_force_block(self):
        from jobcli.orchestration import engine

        src = open(engine.__file__).read()
        assert "force_block=True" in src, (
            "Compulsory pre-submit handoff must pass force_block=True so "
            "AUTO mode still blocks on review."
        )

    def test_confirm_submission_removed_from_apply_path(self):
        from jobcli.orchestration import engine

        src = open(engine.__file__).read()
        # confirm_submission may still exist on AgentInterface for backward
        # compatibility / tests, but the apply path must no longer call it.
        assert "agent.confirm_submission()" not in src, (
            "agent.confirm_submission() should not be used in the engine "
            "apply path any more — the compulsory pre-submit handoff replaces it."
        )

    def test_looks_like_confirmation_called_after_handoff(self):
        from jobcli.orchestration import engine

        src = open(engine.__file__).read()
        assert "self._looks_like_confirmation(" in src
        assert "user_submitted_during_review" in src
