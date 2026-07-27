"""Resolve empty form gaps from resume JSON and persisted field memory.

Memory is used as a *context provider* for the LLM (suggested_value hints,
gap_hints block) — NOT as an autonomous form filler.

Set ``MEMORY_PREFILL_DISABLED = True`` (default) to prevent ``gaps_to_actions``
from generating browser fill actions.  This stops memory-sourced wrong values
from being written into fields before the LLM has a chance to plan, which was
the root cause of the "LLM not filling fields correctly" bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from jobcli.intelligence.memory import AgentMemory
from jobcli.intelligence.synonym_resolver import SynonymResolver
from jobcli.profile.schemas import (
    ATSType,
    ActionType,
    BrowserAction,
    CommonQuestions,
    ResumeData,
    SelectorType,
)
from jobcli.utils.fill_guard import is_reserved_form_value

# ── Master kill-switch for autonomous memory-based form filling ────────────
# When True, ``gaps_to_actions`` returns an empty action list immediately.
# Memory lookups for the LLM (``enrich_gaps_with_suggestions``,
# ``build_gap_hints``) are NOT affected — the LLM still receives memory
# suggestions as context hints via the KNOWN ANSWERS / TARGET GAPS blocks.
# Set to False only if you want to re-enable experimental memory auto-fill.
MEMORY_PREFILL_DISABLED: bool = True

_DROPDOWN_ROLES = frozenset(
    {"combobox", "listbox", "select", "menu", "menuitemradio"}
)


@dataclass
class GapResolution:
    """Result of resolving one empty field gap."""

    label: str
    value: Optional[str]
    source: str
    skipped_reason: Optional[str] = None


class MemoryPrefiller:
    """Turn empty field gaps into browser actions using memory + resume."""

    def __init__(self, synonym_resolver: Optional[SynonymResolver] = None) -> None:
        self.synonym_resolver = synonym_resolver or SynonymResolver()

    @staticmethod
    def _is_dropdown_role(role: str) -> bool:
        return (role or "").lower() in _DROPDOWN_ROLES

    def resolve_gap(
        self,
        label: str,
        role: str,
        options: list[str],
        memory: AgentMemory,
        resume: ResumeData,
        ats_type: ATSType,
        common_questions: Optional[CommonQuestions] = None,
    ) -> GapResolution:
        """Look up the best answer for a single gap label."""
        if not label or not label.strip():
            return GapResolution(label=label, value=None, source="not_found", skipped_reason="empty_label")

        value, source = memory.get_best_answer(
            label, ats_type, resume, common_questions=common_questions
        )
        if not value or is_reserved_form_value(value):
            return GapResolution(
                label=label,
                value=None,
                source=source,
                skipped_reason="no_trusted_answer" if not value else "reserved_value",
            )

        if options and self._is_dropdown_role(role):
            matched = self.synonym_resolver.find_best_option(value, options)
            if matched:
                value = matched
            elif value not in options:
                # Keep raw value — executor fuzzy-match may still succeed.
                pass

        return GapResolution(label=label, value=value, source=source)

    def enrich_gaps_with_suggestions(
        self,
        gaps: list[dict[str, Any]],
        memory: AgentMemory,
        resume: ResumeData,
        ats_type: ATSType,
        common_questions: Optional[CommonQuestions] = None,
    ) -> list[dict[str, Any]]:
        """Add suggested_value / suggested_source to gap rows for LLM auditor."""
        enriched: list[dict[str, Any]] = []
        for gap in gaps:
            row = dict(gap)
            resolution = self.resolve_gap(
                row.get("label") or "",
                row.get("role") or "textbox",
                list(row.get("options") or []),
                memory,
                resume,
                ats_type,
                common_questions=common_questions,
            )
            if resolution.value:
                row["suggested_value"] = resolution.value
                row["suggested_source"] = resolution.source
            enriched.append(row)
        return enriched

    def gaps_to_actions(
        self,
        gaps: list[dict[str, Any]],
        memory: AgentMemory,
        resume: ResumeData,
        ats_type: ATSType,
        common_questions: Optional[CommonQuestions] = None,
    ) -> tuple[list[BrowserAction], list[GapResolution]]:
        """Convert empty gaps into executable BrowserAction list.

        DISABLED when ``MEMORY_PREFILL_DISABLED=True`` (default).

        Memory-sourced values are unreliable as autonomous fills: the label
        matching is approximate and the confidence gate (0.6) is too low,
        causing incorrect values to be written into fields before the LLM
        plans.  The LLM already receives memory as context via
        ``enrich_gaps_with_suggestions`` and ``build_gap_hints`` — those
        paths remain active.
        """
        if MEMORY_PREFILL_DISABLED:
            return [], []
        actions: list[BrowserAction] = []
        resolutions: list[GapResolution] = []

        for gap in gaps:
            label = (gap.get("label") or "").strip()
            if not label:
                continue
            role = (gap.get("role") or "textbox").lower()
            options = list(gap.get("options") or [])
            is_required = bool(gap.get("required"))

            resolution = self.resolve_gap(
                label, role, options, memory, resume, ats_type, common_questions
            )
            resolutions.append(resolution)
            if not resolution.value:
                continue

            action_type = ActionType.SELECT if self._is_dropdown_role(role) else ActionType.FILL
            actions.append(
                BrowserAction(
                    action=action_type,
                    selector=label,
                    selector_type=SelectorType.TEXT,
                    value=resolution.value,
                    field_label=label,
                    required=is_required,
                    confidence=1.0,
                )
            )

        return actions, resolutions
