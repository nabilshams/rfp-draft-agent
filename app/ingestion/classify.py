"""What kind of item an extracted requirement is, how it should be answered, and which items repeat.

Extraction finds the items of an RFP; this module gives each one typed fields instead of free text:

``kind``
    ``question``, ``mandatory`` (a requirement the respondent must meet), ``optional`` (should / may / preferred),
    ``instruction`` (how to write the response: a word limit, a format), ``evaluation_criterion``, ``deadline`` or
    ``submission_rule``. Only the first three are *answered*; the others shape the response or the project record.
``response_type``
    ``narrative``, ``yes_no``, ``table`` or ``attachment``.
``evidence_required``
    whether the RFP asks for supporting evidence (a certificate, a report, references, a case study).
``implicit``
    the requirement is not stated outright and was inferred (only the model sets this).
``duplicate_of``
    the id of the first item this one repeats, so a question that is asked twice is answered once.

These are deterministic rules over the item's own text. The model-based extractor fills the same fields and its values
are kept when valid; where it gives none, or a value outside the lists, these rules decide.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

KINDS = ("question", "mandatory", "optional", "instruction", "evaluation_criterion", "deadline", "submission_rule")
ANSWERED_KINDS = ("question", "mandatory", "optional")
RESPONSE_TYPES = ("narrative", "yes_no", "table", "attachment")

_NUMBER_UNIT = r"\d[\d,]*\s*(?:words?|pages?|characters?|chars|mb|kb|slides?)"
_INSTRUCTION = re.compile(
    rf"(?:no\s+(?:longer|more|greater|larger)\s+than|not\s+(?:exceed|be\s+(?:longer|more)\s+than)|at\s+most|maximum\s+of|up\s+to)\s+{_NUMBER_UNIT}"
    r"|(?:font|typeface|line\s+spacing|margins?|page\s+size|numbered|numbering|headings?)\b"
    r"|\b(?:answers?|responses?|proposals?|submissions?)\s+(?:shall|must|should)\s+(?:be\s+(?:written|presented|structured|formatted|in)|follow|use|use\s+the|begin|start|include\s+the\s+question|reference|cite|restate)"
    r"|\b(?:respondents?|bidders?|offerors?)\s+(?:shall|must|should)\s+(?:answer|number|label|reference|use\s+the\s+(?:template|format|numbering))"
    r"|\bwritten\s+in\s+english\b|\bin\s+the\s+order\s+(?:given|below|listed)\b",
    re.IGNORECASE,
)
_DEADLINE = re.compile(
    r"\b(?:due|deadline|closing\s+(?:date|time)|closes?\s+(?:on|at)|no\s+later\s+than|not\s+later\s+than|on\s+or\s+before|by\s+(?:\d{1,2}[:.]\d{2}|\d{1,2}\s+\w+\s+\d{4}|\w+\s+\d{1,2},?\s+\d{4}))\b",
    re.IGNORECASE,
)
_SUBMISSION = re.compile(
    r"\b(?:submit(?:ted|s)?|submission|lodge(?:d)?|upload(?:ed)?|deliver(?:ed|y)?|sealed|hard\s+cop(?:y|ies)|electronic\s+cop(?:y|ies)|"
    r"tender\s+(?:box|portal)|e-?mail(?:ed)?\s+to|via\s+the\s+(?:portal|tender)|copies\s+of\s+(?:the|your)\s+(?:proposal|response))\b",
    re.IGNORECASE,
)
_DATE_IN_TEXT = re.compile(
    r"(\d{4}-\d{2}-\d{2})|(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})|([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})"
)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_EVALUATION = re.compile(r"\b(?:evaluat(?:ed|ion)|scor(?:ed|ing)|weight(?:ed|ing)|points?\b|marks?\b|selection\s+criteri)", re.IGNORECASE)
_OPTIONAL = re.compile(r"\b(?:should|may|preferabl[ey]|desirabl[ey]|desirable|nice\s+to\s+have|optional(?:ly)?|ideally|would\s+be\s+(?:an\s+)?advantage)\b", re.IGNORECASE)
_MANDATORY = re.compile(r"\b(?:shall|must|is\s+required|are\s+required|will\s+be\s+required|mandatory|required\s+to)\b", re.IGNORECASE)

_YES_NO_START = re.compile(
    r"^(?:please\s+)?(?:do|does|did|is|are|was|were|can|could|will|would|has|have|had|shall|should|may)\s+(?:you|your|the|it|this|that|there|we|any|all|each)\b",
    re.IGNORECASE,
)
_YES_NO_WORDS = 28
_TABLE = re.compile(
    r"\b(?:in\s+a\s+table|tabulate|table\s+(?:of|showing)|matrix|schedule\s+of|list\s+(?:each|all|the)\b.*\b(?:with|including|showing)|"
    r"(?:rate|price|pricing)\s+(?:card|schedule|table)|complete\s+the\s+(?:table|form|schedule))\b",
    re.IGNORECASE,
)
_ATTACHMENT = re.compile(
    r"\b(?:attach(?:ed|ment)?|append(?:ix|ed)|enclose[d]?|supply\s+(?:a\s+)?cop(?:y|ies)|provide\s+(?:a\s+)?(?:copy|copies)\s+of|"
    r"(?:certificate|resume|resum[eé]|cv|curriculum\s+vitae|financial\s+statements?|insurance\s+certificate)s?\s+(?:must|shall|should|to)\s+be\s+(?:attached|provided|supplied|included))\b",
    re.IGNORECASE,
)
_EVIDENCE = re.compile(
    r"\b(?:evidence|proof|supporting\s+(?:document|material)s?|documentation\s+(?:of|to\s+support)|certificat(?:e|ion|ed)s?|"
    r"audit(?:ed)?\s+(?:report|statement)s?|references?|case\s+stud(?:y|ies)|testimonials?|accreditation|attestation|"
    r"copy\s+of|report\s+(?:from|of)|demonstrate|provide\s+(?:a\s+)?(?:copy|report)|"
    r"(?:provide|supply|submit|produce)\s+(?:a|an|the|your|current)\s+(?:[\w.-]+\s+){0,5}(?:report|certificate|certification|attestation|audit))\b",
    re.IGNORECASE,
)

_STOP = frozenset(
    "a an and are as at be been by do does for from has have how in is it its of on or shall should must will would can could "
    "that the their this to we what when where which who with you your our vendor provider contractor supplier respondent "
    "please describe explain provide state confirm".split()
)
_WORD = re.compile(r"[a-z0-9]+")
MIN_DUPLICATE_WORDS = 3
DUPLICATE_SIMILARITY = 0.85


def _is_question(text: str) -> bool:
    return text.strip().endswith("?")


def parse_date(text: str) -> str:
    """The first date in *text* as ``YYYY-MM-DD``, or ``""`` (a date that does not exist is not a date)."""
    for match in _DATE_IN_TEXT.finditer(text or ""):
        try:
            if match.group(1):
                return datetime.strptime(match.group(1), "%Y-%m-%d").date().isoformat()
            if match.group(2):
                day, month, year = int(match.group(2)), _MONTHS.get(match.group(3)[:3].lower()), int(match.group(4))
            else:
                month, day, year = _MONTHS.get(match.group(5)[:3].lower()), int(match.group(6)), int(match.group(7))
            if month:
                return date(year, month, day).isoformat()
        except ValueError:
            continue
    return ""


def kind_of(text: str, priority: str = "standard") -> str:
    """The kind of an item from its own wording. A question stays a question whatever else it says."""
    if _is_question(text):
        return "question"
    if _DEADLINE.search(text) and (parse_date(text) or re.search(r"\b(?:deadline|closing|due)\b", text, re.IGNORECASE)):
        return "deadline"
    if _INSTRUCTION.search(text):
        return "instruction"
    if _SUBMISSION.search(text) and not re.search(r"\b(?:solution|platform|system|service|software)\b", text, re.IGNORECASE):
        return "submission_rule"
    if _EVALUATION.search(text) and not _MANDATORY.search(text):
        return "evaluation_criterion"
    if priority in ("preferred", "optional") or (_OPTIONAL.search(text) and not _MANDATORY.search(text)):
        return "optional"
    return "mandatory"


def response_type_of(text: str, kind: str) -> str:
    if kind not in ANSWERED_KINDS:
        return "narrative"
    if _ATTACHMENT.search(text):
        return "attachment"
    if _TABLE.search(text):
        return "table"
    if kind == "question" and _YES_NO_START.search(text.strip()) and len(text.split()) <= _YES_NO_WORDS:
        return "yes_no"
    return "narrative"


def evidence_required_of(text: str, kind: str) -> bool:
    return kind in ANSWERED_KINDS and bool(_EVIDENCE.search(text))


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOP]


def similarity(a: str, b: str) -> float:
    """How alike two items are (Jaccard over their content words); 1.0 means the same words."""
    left, right = set(_words(a)), set(_words(b))
    if len(left) < MIN_DUPLICATE_WORDS or len(right) < MIN_DUPLICATE_WORDS:
        return 0.0
    return len(left & right) / len(left | right)


def find_duplicates(items: list[dict[str, Any]]) -> dict[str, str]:
    """``{id of a repeat: id of the first item it repeats}``, among the items that are answered.

    An item repeats an earlier one when their content words are (nearly) the same and the response type is the same.
    The first is the one that is answered; the repeats take its answer, so the same question never gets two answers.
    """
    primaries: list[dict[str, Any]] = []
    groups: dict[str, str] = {}
    for item in items:
        if item.get("kind") not in ANSWERED_KINDS:
            continue
        match = next(
            (p for p in primaries if p.get("response_type") == item.get("response_type")
             and similarity(p["text"], item["text"]) >= DUPLICATE_SIMILARITY),
            None,
        )
        if match:
            groups[item["id"]] = match["id"]
        else:
            primaries.append(item)
    return groups


def clean(value: Any, allowed: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return text if text in allowed else default
