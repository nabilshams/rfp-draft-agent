"""Keeping invented numbers out of a draft.

A model asked for a persuasive RFP response will happily write "improved inventory turnover by 15%" with
nothing behind it. Prompts tell it not to, but a prompt is a request, not a check. This module is the check:
every statistic, amount, multiplier or customer/experience count in generated text must also appear in the
material the model was given (retrieved sources, the requirement, the user's context, the RFP's guidance).
A sentence with a figure that is not in that material is replaced by a visible marker for human review.

It looks at *figures* only. It cannot tell whether a sentence without numbers is true; that needs the claim
verifier (see issue #35).
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

REMOVED_MARKER = "[Statement removed: it contained figures that are not in the sources – recommend human review.]"

_NUMBER = r"\d+(?:,\d{3})*(?:\.\d+)?"
_FIGURE_RE = re.compile(
    rf"(?P<money>[$€£]\s?{_NUMBER}(?:\s?(?:k|m|bn|million|billion)\b)?)"
    rf"|(?P<percent>{_NUMBER}\s?(?:%|percent\b|per cent\b))"
    rf"|(?P<multiple>{_NUMBER}\s?(?:x\b|-fold\b|times\b))"
    rf"|(?P<count>{_NUMBER}\+?\s+(?:years?|months?|clients?|customers?|projects?|deployments?|engagements?"
    rf"|implementations?|organi[sz]ations?|users|employees|consultants|engineers|countries)\b)",
    re.IGNORECASE,
)
_MAGNITUDES = {"k": "k", "m": "m", "million": "m", "bn": "bn", "billion": "bn"}
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _canonical(match: re.Match) -> str:
    """A form of the figure that ignores spacing, commas, "percent" vs "%" and plurals."""
    text = match.group().lower()
    number = re.search(_NUMBER, text).group().replace(",", "")
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    kind = match.lastgroup
    if kind == "money":
        magnitude = re.search(r"[a-z]+$", text)
        return f"{text[0]}{number}{_MAGNITUDES.get(magnitude.group(), '') if magnitude else ''}"
    if kind == "percent":
        return f"{number}%"
    if kind == "multiple":
        return f"{number}x"
    return f"{number} {re.sub(r'[^a-z]', '', text).rstrip('s')}"


def figures_in(text: str) -> set[str]:
    return {_canonical(m) for m in _FIGURE_RE.finditer(text or "")}


def remove_unsupported_figures(text: str, sources: str) -> tuple[str, list[str]]:
    """*text* with every sentence that has a figure missing from *sources* replaced by a marker.

    Returns the new text and the figures that were not supported (as written, in order).
    """
    if not text or not _FIGURE_RE.search(text):
        return text, []

    known = figures_in(sources)
    removed: list[str] = []
    lines: list[str] = []
    for line in text.split("\n"):
        kept: list[str] = []
        for sentence in _SENTENCE_END.split(line):
            unsupported = [m.group() for m in _FIGURE_RE.finditer(sentence) if _canonical(m) not in known]
            if not unsupported:
                kept.append(sentence)
                continue
            removed.extend(unsupported)
            if not kept or kept[-1] != REMOVED_MARKER:
                kept.append(REMOVED_MARKER)
        lines.append(" ".join(kept))
    return "\n".join(lines), removed


def ground_fields(item: dict[str, Any], fields: tuple[str, ...], sources: str) -> list[str]:
    """Apply :func:`remove_unsupported_figures` to the named text fields of *item* (in place).

    Returns the figures removed.
    """
    removed: list[str] = []
    for name in fields:
        value = item.get(name)
        if isinstance(value, str):
            item[name], gone = remove_unsupported_figures(value, sources)
            removed.extend(gone)
    if removed:
        logger.warning("Removed %d unsupported figure(s) from generated text: %s", len(removed), removed[:10])
    return removed


def note_removed(answer: dict[str, Any], removed: list[str]) -> None:
    """Tell the reviewer, in the answer's own ``risks``, that figures were taken out."""
    if removed:
        unique = ", ".join(dict.fromkeys(removed))
        answer.setdefault("risks", []).append(
            f"Figures removed because they are not in the sources: {unique}. Add them from a source or leave them out."
        )
