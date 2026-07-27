from typing import Literal

FieldType = Literal[
    "text",
    "radio",
    "checkbox",
    "select",
    "multiselect",
]


def classify_field(field_name: str) -> FieldType:
    """Simple rule-based field classifier."""

    name = field_name.lower()

    if any(x in name for x in ["gender", "sex"]):
        return "radio"

    if any(x in name for x in ["agree", "accept", "consent", "terms"]):
        return "checkbox"

    if any(x in name for x in ["skills", "technologies", "languages"]):
        return "multiselect"

    if any(x in name for x in ["country", "state", "city"]):
        return "select"

    return "text"