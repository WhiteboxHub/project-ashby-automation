"""Tool (function-calling) schemas for each LLM provider.

Defines a single ``fill_form_field`` tool that the LLM calls once per empty
form field.  Exporting one canonical dict and converting it per-provider keeps
all three provider implementations in sync automatically.

Usage::

    from jobcli.llm.tool_schemas import (
        FILL_FORM_FIELD_OPENAI,
        FILL_FORM_FIELD_ANTHROPIC,
        build_gemini_tool,
    )
"""
from __future__ import annotations

# ── Canonical parameter schema (JSON Schema draft-7 subset) ──────────────────
_FILL_FIELD_PARAMS: dict = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["fill", "type", "select", "click", "upload", "ask"],
            "description": (
                "Browser action type. Use 'fill'/'type' for text inputs; "
                "'select' for dropdowns, radios, and checkboxes; "
                "'click' for navigation buttons; 'upload' for file inputs; "
                "'ask' for open-ended essay questions."
            ),
        },
        "field_label": {
            "type": "string",
            "description": "Exact visible label of the field as it appears on the page.",
        },
        "selector": {
            "type": "string",
            "description": (
                "Exact accessible name from the AX tree snapshot. "
                "Matches field_label unless a CSS/XPath selector is required."
            ),
        },
        "selector_type": {
            "type": "string",
            "enum": ["text", "role", "aria_label", "css", "xpath"],
            "description": "Element location strategy. Default: 'text'.",
        },
        "value": {
            "type": "string",
            "description": (
                "Value to enter or select. "
                "For dropdowns the value MUST exactly match one of the listed options. "
                "For yes/no radios use exactly 'Yes' or 'No'."
            ),
        },
        "confidence": {
            "type": "number",
            "description": (
                "Confidence score 0.0–1.0. "
                "Use ≥0.9 for resume-backed values, 0.75 for reasonably inferred values."
            ),
        },
        "thought": {
            "type": "string",
            "description": "One-sentence reasoning explaining why this value was chosen.",
        },
    },
    "required": ["action", "field_label", "selector", "value"],
}

_TOOL_DESCRIPTION = (
    "Perform a single browser action on one job application form field. "
    "Call this function once for EACH empty field listed in TARGET GAPS. "
    "Do NOT call it for fields already listed in ALREADY FILLED."
)

# ── OpenAI format ─────────────────────────────────────────────────────────────
FILL_FORM_FIELD_OPENAI: dict = {
    "type": "function",
    "function": {
        "name": "fill_form_field",
        "description": _TOOL_DESCRIPTION,
        "parameters": _FILL_FIELD_PARAMS,
    },
}

# ── Anthropic format ──────────────────────────────────────────────────────────
FILL_FORM_FIELD_ANTHROPIC: dict = {
    "name": "fill_form_field",
    "description": _TOOL_DESCRIPTION,
    "input_schema": _FILL_FIELD_PARAMS,
}


def build_gemini_tool():
    """Build a Gemini v2 ``types.Tool`` for native function calling.

    Constructed lazily (not at import time) so the google-genai import does not
    fail for users who haven't installed or configured the Gemini SDK.

    Compatible with google-genai ≥ 1.0 (installed version: 2.8.0).
    """
    from google.genai import types as genai_types  # noqa: PLC0415

    func_decl = genai_types.FunctionDeclaration(
        name="fill_form_field",
        description=_TOOL_DESCRIPTION,
        parameters={
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "enum": ["fill", "type", "select", "click", "upload", "ask"],
                    "description": _FILL_FIELD_PARAMS["properties"]["action"]["description"],
                },
                "field_label": {
                    "type": "STRING",
                    "description": _FILL_FIELD_PARAMS["properties"]["field_label"]["description"],
                },
                "selector": {
                    "type": "STRING",
                    "description": _FILL_FIELD_PARAMS["properties"]["selector"]["description"],
                },
                "selector_type": {
                    "type": "STRING",
                    "enum": ["text", "role", "aria_label", "css", "xpath"],
                    "description": _FILL_FIELD_PARAMS["properties"]["selector_type"]["description"],
                },
                "value": {
                    "type": "STRING",
                    "description": _FILL_FIELD_PARAMS["properties"]["value"]["description"],
                },
                "confidence": {
                    "type": "NUMBER",
                    "description": _FILL_FIELD_PARAMS["properties"]["confidence"]["description"],
                },
                "thought": {
                    "type": "STRING",
                    "description": _FILL_FIELD_PARAMS["properties"]["thought"]["description"],
                },
            },
            "required": ["action", "field_label", "selector", "value"],
        },
    )
    return genai_types.Tool(function_declarations=[func_decl])
