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

# Whether an item is about *the response itself* (its answers, proposal, submission) rather than about the product or
# the vendor's service. Instructions, deadlines and submission rules are about the response; "API responses must be in
# JSON" and "the vendor shall submit monthly reports" are requirements and must be drafted.
_RESPONSE_SUBJECT = re.compile(
    r"\b(?:(?P<qualifier>api|http|server|system|error|network|rest|database|service|web|software)\s+)?"
    r"(?:answers?|responses?|proposals?|tenders?|bids?|submissions?|replies|reply|respondents?|bidders?|tenderers?|offerors?|quotations?)\b",
    re.IGNORECASE,
)
_LENGTH_LIMIT = re.compile(
    r"(?:no\s+(?:longer|more|greater|larger)\s+than|not\s+(?:exceed|be\s+(?:longer|more)\s+than)|maximum\s+of|at\s+most|up\s+to|limited\s+to)"
    r"\s+\d[\d,]*\s*(?:words?|pages?|characters|slides?)\b",
    re.IGNORECASE,
)
_FORMATTING = re.compile(
    r"\b(?:font|typeface|line\s+spacing|margins?|page\s+size|numbered|numbering|headings?|single[- ]spaced|double[- ]spaced)\b", re.IGNORECASE
)
_RESPONSE_RULE = re.compile(
    r"\b(?:be\s+(?:written|presented|structured|formatted|in\s+the\s+(?:order|format|template))|follow\s+the|use\s+the\s+(?:template|format|numbering)|"
    r"begin\s+with|restate|reference\s+the\s+(?:question|requirement)|written\s+in\s+english|(?:in|to)\s+the\s+order\s+(?:given|below|listed)|"
    r"(?:answer|number|label)\s+(?:each|every|all))\b",
    re.IGNORECASE,
)
_DEADLINE_WORDS = re.compile(
    r"\b(?:deadline|closing\s+(?:date|time)|closes?\b|due\s+(?:date|on|by|before)|(?:is|are)\s+due\b|submission\s+date)",
    re.IGNORECASE,
)
_DATE_PHRASE = re.compile(
    r"\b(?:by|before|no\s+later\s+than|not\s+later\s+than|on\s+or\s+before|until)\b", re.IGNORECASE
)
_SUBMIT_VERBS = re.compile(r"\b(?:submit(?:ted)?|lodge[d]?|received|returned|delivered|e-?mailed|uploaded|sent|provided\s+to)\b", re.IGNORECASE)
_SUBMISSION_STRONG = re.compile(
    r"\b(?:tender\s+(?:box|portal)|hard\s+cop(?:y|ies)|electronic\s+cop(?:y|ies)|sealed\s+envelope|"
    r"cop(?:y|ies)\s+of\s+(?:the|your)\s+(?:proposal|response|tender|bid))\b",
    re.IGNORECASE,
)
_SUBMISSION_VERBS = re.compile(
    r"\b(?:submit(?:ted)?|submission|lodge[d]?|upload(?:ed)?|e-?mail(?:ed)?\s+to|deliver(?:ed)?\s+(?:to|by|via))\b", re.IGNORECASE
)
_DATE_IN_TEXT = re.compile(
    r"(\d{4}-\d{2}-\d{2})|(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})|([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})"
)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_EVALUATION = re.compile(
    r"\b(?:scor(?:ed|ing)|weight(?:ed|ing)|evaluat(?:ed|ion)\s+(?:criteri|on|will)|selection\s+criteri|"
    r"\d+\s*(?:points|marks|percent|%)\s+(?:for|of|will|each))",
    re.IGNORECASE,
)
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


def _about_the_response(text: str) -> bool:
    """Whether the text is about the response itself ("each answer", "proposals") and not about the product."""
    return any(not m.group("qualifier") for m in _RESPONSE_SUBJECT.finditer(text))


def kind_of(text: str, priority: str = "standard") -> str:
    """The kind of an item from its own wording. A question stays a question whatever else it says.

    Getting this wrong in one direction is costly: an item that is not an ``answered`` kind is not drafted. So an
    instruction, date or submission rule has to be about the response itself (its answers, proposal or submission);
    a shall/must statement about the product or the vendor is a requirement, whatever words it shares with them.
    """
    if _is_question(text):
        return "question"
    about = _about_the_response(text)
    modal_requirement = bool(_MANDATORY.search(text) or _OPTIONAL.search(text)) and not about
    if about:
        dated = bool(parse_date(text))
        if _DEADLINE_WORDS.search(text) or (dated and _DATE_PHRASE.search(text) and _SUBMIT_VERBS.search(text)):
            return "deadline"
        if _LENGTH_LIMIT.search(text) or _RESPONSE_RULE.search(text) or _FORMATTING.search(text):
            return "instruction"
    if (_SUBMISSION_STRONG.search(text) and (about or _SUBMISSION_VERBS.search(text))) or (
        about and _SUBMISSION_VERBS.search(text)
    ):
        return "submission_rule"
    if _DEADLINE_WORDS.search(text) and parse_date(text) and not modal_requirement:
        return "deadline"
    if _EVALUATION.search(text) and not modal_requirement and not _MANDATORY.search(text):
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


_NUMBERS = re.compile(r"\d+(?:[.,]\d+)*|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty|fifty|hundred|thousand|first|second|third)\b", re.IGNORECASE)
_NEGATIONS = re.compile(r"\b(?:not|no|never|without|cannot|neither|nor|none|n't)\b", re.IGNORECASE)


def _same_figures(a: str, b: str) -> bool:
    """Items that differ in a number or in a negation ask for different things, however alike the rest of the words."""
    return (
        {m.lower() for m in _NUMBERS.findall(a)} == {m.lower() for m in _NUMBERS.findall(b)}
        and bool(_NEGATIONS.search(a)) == bool(_NEGATIONS.search(b))
    )


def similarity(a: str, b: str) -> float:
    """How alike two items are (Jaccard over their content words); 1.0 means the same words."""
    left, right = set(_words(a)), set(_words(b))
    if len(left) < MIN_DUPLICATE_WORDS or len(right) < MIN_DUPLICATE_WORDS or not _same_figures(a, b):
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
