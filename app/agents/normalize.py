"""Shape checks for what the model returns.

The model is asked for JSON, but nothing guarantees it: fields can be missing, the wrong type, or (after an
injection attempt) carry values the rest of the application would mishandle, e.g. a compliance value other
than Yes/Partial/No reaching the spreadsheet. These helpers coerce a parsed reply to the documented shape.
"""

from __future__ import annotations

from typing import Any

_MEETS = {"yes": "Yes", "partial": "Partial", "no": "No"}
_LIST_FIELDS = ("assumptions", "risks", "followups")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [_text(v) for v in value if v is not None and _text(v).strip()]
    return [_text(value)]


def _evidence(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [
        {"title": _text(e.get("title")), "source_url": _text(e.get("source_url")), "quote": _text(e.get("quote"))}
        for e in value
        if isinstance(e, dict)
    ]


def normalize_answer(answer: dict[str, Any], *, require_meets: bool = True) -> dict[str, Any]:
    """Coerce an answer object to the documented schema, keeping any extra keys it carries.

    An unrecognised compliance value becomes "Partial" (never an unearned "Yes") with a risk noting it.
    """
    result = dict(answer)
    result["answer_markdown"] = _text(answer.get("answer_markdown"))
    result["evidence"] = _evidence(answer.get("evidence"))
    for key in _LIST_FIELDS:
        result[key] = _strings(answer.get(key))

    if require_meets or "meets" in answer:
        meets = _MEETS.get(_text(answer.get("meets")).strip().lower())
        if meets is None:
            meets = "Partial"
            result["risks"].append("The model returned an unrecognised compliance value; please review.")
        result["meets"] = meets
    return result


def normalize_draft(draft: dict[str, Any]) -> dict[str, Any]:
    """Coerce a full draft: text summaries and a list of normalised response objects."""
    result = dict(draft)
    result["executive_summary"] = _text(draft.get("executive_summary"))
    result["solution_overview"] = _text(draft.get("solution_overview"))
    responses = draft.get("responses")
    result["responses"] = [normalize_answer(r) for r in responses if isinstance(r, dict)] if isinstance(responses, list) else []
    return result


def normalize_section(
    section: dict[str, Any], *, section_name: str, req_ids: list[str] | None = None
) -> dict[str, Any]:
    """Coerce a section narrative: the name (and IDs, when given) are the ones we asked for, the text a string."""
    result = dict(section)
    result["section_name"] = section_name
    result["narrative_markdown"] = _text(section.get("narrative_markdown"))
    if req_ids is not None:
        result["requirement_ids"] = req_ids
    return result
