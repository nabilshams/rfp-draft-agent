"""What state an answer is in, and the rule that nothing is invented when the evidence isn't there.

A drafted answer is more than ``meets: Yes/Partial/No``. The *state* says how far it can be trusted and who has to act:

==========================  ==============================================================================
``fully_answered``          grounded in retrieved sources (cited, with the quote found in the source)
``partially_answered``      grounded, but part of the requirement is not covered
``conflicting_evidence``    the sources disagree; the draft follows the more authoritative one and a person decides
``no_evidence``             nothing in the knowledge base covers it: a placeholder, no prose
``needs_sme``               a subject-matter expert has to supply or confirm it: a placeholder, no prose
``needs_commercial``        pricing and commercial terms: never drafted, always a placeholder
``needs_legal``             contractual and legal terms: never drafted, always a placeholder
``failed``                  the draft could not be generated (an error or a timeout), recorded as such, not as "No"
==========================  ==============================================================================

``meets`` stays the compliance claim and is separate: a placeholder makes no claim, so its ``meets`` is empty.

**The hard rule.** Where the evidence is insufficient the answer text is a placeholder such as
``[SME INPUT REQUIRED: <what is missing>]``, and nothing the model wrote is kept. Evidence is insufficient when
nothing was retrieved (the model isn't even asked), when the model reports that the sources don't cover the
requirement, when the answer opens by saying so, or when it cites nothing that really appears in a retrieved source.
Whether a *cited* source actually supports a claim is judged claim by claim by the verifier (issue #35); this rule is the
floor underneath it.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import Any

from app.agents import kb_search

FULLY = "fully_answered"
PARTIAL = "partially_answered"
CONFLICTING = "conflicting_evidence"
NO_EVIDENCE = "no_evidence"
NEEDS_SME = "needs_sme"
NEEDS_COMMERCIAL = "needs_commercial"
NEEDS_LEGAL = "needs_legal"
FAILED = "failed"

STATES = (FULLY, PARTIAL, CONFLICTING, NO_EVIDENCE, NEEDS_SME, NEEDS_COMMERCIAL, NEEDS_LEGAL, FAILED)
# States that carry a written answer
ANSWERED = (FULLY, PARTIAL, CONFLICTING)
# States whose answer text is only a placeholder
PLACEHOLDER_STATES = (NO_EVIDENCE, NEEDS_SME, NEEDS_COMMERCIAL, NEEDS_LEGAL)
# States that wait for a person before the requirement can be approved
NEEDS_PERSON = PLACEHOLDER_STATES + (CONFLICTING, FAILED)
# What a person may approve as it stands
APPROVABLE = (FULLY, PARTIAL)

LABELS = {
    FULLY: "Fully answered",
    PARTIAL: "Partially answered",
    CONFLICTING: "Conflicting evidence",
    NO_EVIDENCE: "No evidence",
    NEEDS_SME: "Needs SME",
    NEEDS_COMMERCIAL: "Needs commercial",
    NEEDS_LEGAL: "Needs legal",
    FAILED: "Failed",
}

_TAGS = {
    NO_EVIDENCE: "SME INPUT REQUIRED",
    NEEDS_SME: "SME INPUT REQUIRED",
    NEEDS_COMMERCIAL: "COMMERCIAL INPUT REQUIRED",
    NEEDS_LEGAL: "LEGAL INPUT REQUIRED",
}
_DEFAULT_MISSING = {
    NO_EVIDENCE: "no source in the knowledge base covers this requirement",
    NEEDS_SME: "a subject-matter expert must provide or confirm this",
    NEEDS_COMMERCIAL: "pricing and commercial terms are answered by the commercial team, not drafted",
    NEEDS_LEGAL: "contractual and legal terms are answered by legal, not drafted",
}

# What the model may report about how well the sources cover a requirement (the ``sufficiency`` field)
SUFFICIENCY = {
    "complete": FULLY, "partial": PARTIAL, "none": NO_EVIDENCE, "needs_sme": NEEDS_SME, "conflict": CONFLICTING,
}
_MAX_MISSING = 200
_ABSTAIN_START = re.compile(r"^\W*(?:not provided in sources|no relevant (?:source|evidence|information))", re.IGNORECASE)
_ABSTAIN_ANYWHERE = re.compile(r"not provided in sources|recommend human review", re.IGNORECASE)


def placeholder(state: str, missing: str = "") -> str:
    """The text that stands in for an answer a person has to supply, e.g. ``[SME INPUT REQUIRED: <what is missing>]``."""
    detail = clean_missing(missing) or _DEFAULT_MISSING[state]
    return f"[{_TAGS[state]}: {detail}]"


def clean_missing(value: Any) -> str:
    """A model's description of what is missing, made safe to put inside a placeholder: one short line, no brackets."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split()).replace("[", "(").replace("]", ")").strip(" .")
    return text[:_MAX_MISSING]


def has_placeholder(text: str) -> bool:
    return bool(re.match(r"^\s*\[(?:SME|COMMERCIAL|LEGAL) INPUT REQUIRED:", text or ""))


# ── Routing: some requirements are never drafted ─────────


@dataclass(frozen=True)
class Router:
    """Sends pricing and contractual requirements to the people who answer them. Terms come from configuration."""

    commercial: re.Pattern[str] | None
    legal: re.Pattern[str] | None

    def route(self, requirement: dict[str, Any]) -> str | None:
        """``needs_legal`` / ``needs_commercial`` for a requirement that is about them, else ``None``.

        The requirement's text, category and source section are all read, so a row filed under "Pricing" is routed
        even when its own wording is neutral.
        """
        text = " ".join(str(requirement.get(k) or "") for k in ("text", "category", "source_section"))
        if self.legal and self.legal.search(text):
            return NEEDS_LEGAL
        if self.commercial and self.commercial.search(text):
            return NEEDS_COMMERCIAL
        return None


def _pattern(terms: str) -> re.Pattern[str] | None:
    """Terms separated by ';'. ``word*`` matches any word starting that way; otherwise the whole word or phrase.

    A term does not match inside a hyphenated word (``cost-effective`` is not about cost).
    """
    parts: list[str] = []
    for raw in terms.split(";"):
        term = raw.strip().lower()
        if not term:
            continue
        prefix = term.endswith("*")
        body = re.escape(term.rstrip("*")).replace(r"\ ", r"\s+")
        parts.append(body + (r"[a-z]*" if prefix else "") + r"(?![\w-])")
    if not parts:
        return None
    return re.compile(r"(?<![\w-])(?:" + "|".join(parts) + ")", re.IGNORECASE)


@functools.lru_cache(maxsize=8)
def router_for(commercial_terms: str, legal_terms: str) -> Router:
    return Router(_pattern(commercial_terms), _pattern(legal_terms))


def router_from_settings(settings: Any) -> Router:
    return router_for(settings.route_commercial_terms, settings.route_legal_terms)


# ── Building answers ─────────────────────────────────────


def placeholder_answer(
    requirement: dict[str, Any], state: str, *, missing: str = "", followups: list[str] | None = None,
    kb_evidence_found: bool = False,
) -> dict[str, Any]:
    """An answer that says a person has to supply this. Makes no compliance claim (``meets`` is empty)."""
    return {
        "requirement_id": requirement.get("id", "?"),
        "requirement_text": requirement.get("text", ""),
        "answer_markdown": placeholder(state, missing),
        "meets": "",
        "evidence": [],
        "assumptions": [],
        "risks": [],
        "followups": list(followups or [])[:5],
        "kb_evidence_found": kb_evidence_found,
        "state": state,
        "missing": clean_missing(missing) or _DEFAULT_MISSING[state],
    }


def pre_gate(
    requirement: dict[str, Any], *, router: Router, evidence_found: bool, web_search: bool
) -> dict[str, Any] | None:
    """An answer for a requirement the model must not be asked about, or ``None`` to go ahead and draft it.

    * pricing and contractual requirements are routed to the people who answer them;
    * with nothing retrieved from the knowledge base (and no web grounding to fall back on) there is nothing to draft
      from, so the model isn't asked, which also saves the call.
    """
    routed = router.route(requirement)
    if routed:
        return placeholder_answer(requirement, routed, kb_evidence_found=evidence_found)
    if not evidence_found and not web_search:
        return placeholder_answer(requirement, NO_EVIDENCE)
    return None


_TYPOGRAPHY = str.maketrans(
    {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "\u00a0": " "}
)
_MARKDOWN = re.compile(r"[*_`#>|]")
_ELLIPSIS = re.compile(r"\u2026|\.\.\.")
MIN_QUOTE = 10  # shorter quotes prove nothing


def _squash(text: Any) -> str:
    """Text lower-cased with whitespace collapsed, typographic quotes and dashes made plain and markdown marks removed."""
    plain = _MARKDOWN.sub(" ", str(text or "").translate(_TYPOGRAPHY))
    return " ".join(plain.lower().split())


def _quote_in(quote: Any, content: str) -> bool:
    """Whether a quote is in a document. A quote with an ellipsis is checked fragment by fragment."""
    fragments = [f for f in (_squash(p) for p in _ELLIPSIS.split(str(quote or ""))) if len(f) >= MIN_QUOTE]
    return bool(fragments) and all(f in content for f in fragments)


def count_supported_citations(answer: dict[str, Any], docs: list[dict[str, Any]]) -> tuple[int, int]:
    """(evidence entries, entries whose quote really appears in the retrieved document they cite).

    Links are compared as the citation check does (percent-encoding, trailing slash, a name cut at a parenthesis).
    """
    contents: dict[str, list[str]] = {}
    for doc in docs:
        url = kb_search._normalize_url(str(doc.get("source_url", "")))
        if url:
            contents.setdefault(url, []).append(_squash(doc.get("content", "")))
    entries = [e for e in answer.get("evidence", []) if isinstance(e, dict)]
    supported = 0
    for entry in entries:
        url = str(entry.get("source_url", ""))
        if any(
            kb_search._is_allowed(url, {known}) and any(_quote_in(entry.get("quote", ""), text) for text in texts)
            for known, texts in contents.items()
        ):
            supported += 1
    return len(entries), supported


def finalize(
    answer: dict[str, Any],
    requirement: dict[str, Any],
    *,
    docs: list[dict[str, Any]],
    web_grounded: bool = False,
) -> dict[str, Any]:
    """The answer with its state decided, and replaced by a placeholder if the evidence is insufficient.

    The model's own report (``sufficiency``, ``missing``) is read, then checked: an answer that cites nothing which
    appears in a retrieved source, or that opens by saying the sources don't cover it, is never kept as prose.
    """
    hint = SUFFICIENCY.get(str(answer.get("sufficiency", "")).strip().lower())
    missing = answer.get("missing", "")
    text = str(answer.get("answer_markdown", ""))
    if not text.strip() and hint not in (NO_EVIDENCE, NEEDS_SME):
        return failed_answer(
            requirement, message="The model returned no answer text.", risk="The model returned an empty answer",
            kb_evidence_found=bool(answer.get("kb_evidence_found", False)),
        )
    _, supported = count_supported_citations(answer, docs)
    grounded = supported > 0 or web_grounded

    if hint == NO_EVIDENCE or _ABSTAIN_START.search(text):
        state = NO_EVIDENCE
    elif hint == NEEDS_SME:
        state = NEEDS_SME
    elif not grounded:
        state = NO_EVIDENCE
    elif hint == CONFLICTING:
        state = CONFLICTING
    elif hint == PARTIAL or str(answer.get("meets", "")) == "Partial" or _ABSTAIN_ANYWHERE.search(text):
        state = PARTIAL
    else:
        state = FULLY

    if state in PLACEHOLDER_STATES:
        return placeholder_answer(
            requirement, state, missing=missing if isinstance(missing, str) else "",
            followups=answer.get("followups") or [], kb_evidence_found=bool(answer.get("kb_evidence_found", False)),
        )

    result = dict(answer)
    result.pop("sufficiency", None)
    result["state"] = state
    result["missing"] = clean_missing(missing)
    return result


def failed_answer(requirement: dict[str, Any], *, message: str, risk: str, kb_evidence_found: bool = False) -> dict[str, Any]:
    """The record of a requirement whose generation failed. It is a failure, not an answer, and makes no claim."""
    return {
        "requirement_id": requirement.get("id", "?"),
        "requirement_text": requirement.get("text", ""),
        "answer_markdown": message,
        "meets": "",
        "evidence": [],
        "assumptions": [],
        "risks": [risk],
        "followups": [],
        "kb_evidence_found": kb_evidence_found,
        "state": FAILED,
        "missing": "",
        # Marks a placeholder for a failure: a project draft does not store it as the answer, so a later run tries again
        "generation_failed": True,
    }
