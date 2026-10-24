"""Claim-level verification: what an answer claims, and whether the evidence it cites supports each claim.

The writer lists the claims its answer makes, each citing evidence ids. Then, independently of the writer:

1. **Deterministic checks first, no model.** A claim that cites nothing, or whose cited quotes are not really in the
   retrieved source (a fabricated or paraphrased quote), or that states a figure the cited passages do not contain, is
   ``unsupported`` on the spot.
2. **A separate verifier** (its own prompt, and optionally its own model deployment) judges the remaining claims
   against the passages they cite: ``supported``, ``partial``, ``unsupported`` or ``contradicted``.

Nothing is rewritten or removed here (that is a decision for a person, and for issue #16). The result is attached to
the answer: each claim with its verdict, reason and evidence ids, and an overall ``verification`` status:

==========================  ==============================================================================
``verified``                every claim is supported
``partially_verified``      nothing is unsupported or contradicted, but some claims are partial or could not be checked
``flagged``                 at least one claim is unsupported or contradicted; the requirement cannot be approved
``unchecked``               nothing could be checked (no claims listed, verification off, or the verifier failed)
==========================  ==============================================================================

A contradicted claim makes the answer's state ``conflicting_evidence``; an unsupported one downgrades
``fully_answered`` to ``partially_answered`` (see :mod:`app.agents.states`), and each flagged claim is listed under the
answer's risks so every output shows it.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.agents import grounding, kb_search, lifecycle, runner, states
from app.agents.untrusted import fence
from app.config import get_settings
from app.telemetry import spans

logger = logging.getLogger(__name__)

SUPPORTED, PARTIAL, UNSUPPORTED, CONTRADICTED = "supported", "partial", "unsupported", "contradicted"
VERDICTS = (SUPPORTED, PARTIAL, UNSUPPORTED, CONTRADICTED)
FLAGGED_VERDICTS = (UNSUPPORTED, CONTRADICTED)

VERIFIED, PARTIALLY_VERIFIED, FLAGGED, UNCHECKED = "verified", "partially_verified", "flagged", "unchecked"
STATUSES = (VERIFIED, PARTIALLY_VERIFIED, FLAGGED, UNCHECKED)

MAX_CLAIMS = 25
MAX_CLAIM_CHARS = 500
MAX_REASON_CHARS = 200
MAX_PASSAGE_CHARS = 3000
MAX_RISKS = 5

_EVIDENCE_ID = re.compile(r"^E\d{1,3}$")
_WORD = re.compile(r"[\w.+-]+")


# ── Evidence ids and claims ──────────────────────────────


def assign_evidence_ids(entries: Any) -> list[dict[str, Any]]:
    """The evidence entries with a unique id each (``E1``, ``E2``...). Ids the writer gave are kept when valid."""
    items = [dict(e) for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    used: set[str] = set()
    for item in items:
        eid = str(item.get("id", "")).strip()
        if _EVIDENCE_ID.match(eid) and eid not in used:
            used.add(eid)
            item["id"] = eid
        else:
            item["id"] = ""
    number = 1
    for item in items:
        if not item["id"]:
            while f"E{number}" in used:
                number += 1
            item["id"] = f"E{number}"
            used.add(item["id"])
    return items


def read_claims(raw: Any, evidence_ids: set[str]) -> list[dict[str, Any]]:
    """The writer's claims, cleaned: short text, ids of evidence that exists, at most :data:`MAX_CLAIMS`."""
    claims: list[dict[str, Any]] = []
    for item in (raw if isinstance(raw, list) else [])[:MAX_CLAIMS]:
        data = item if isinstance(item, dict) else {"text": item} if isinstance(item, str) else {}
        text = " ".join(str(data.get("text") or "").split())[:MAX_CLAIM_CHARS]
        if not text:
            continue
        cited = data.get("evidence_ids")
        ids = [str(i).strip() for i in cited] if isinstance(cited, list) else []
        unique = list(dict.fromkeys(i for i in ids if i in evidence_ids))
        claims.append({"id": f"C{len(claims) + 1}", "text": text, "evidence_ids": unique})
    return claims


# ── Deterministic checks ─────────────────────────────────


def rule_verdict(
    claim: dict[str, Any], passages: dict[str, str | None], external: set[str]
) -> tuple[str | None, str] | None:
    """A verdict reached without a model, as ``(verdict, reason)``; ``None`` when a model has to judge.

    ``verdict`` is ``None`` for a claim that can't be checked at all (it rests only on web sources).
    """
    ids = claim["evidence_ids"]
    if not ids:
        return UNSUPPORTED, "The claim cites no evidence."
    if all(i in external for i in ids):
        return None, "Rests on a web source, which can't be checked here."
    found = [passages[i] for i in ids if passages.get(i)]
    if not found:
        return UNSUPPORTED, "The quoted text is not in the cited source."
    missing = sorted(grounding.figures_in(claim["text"]) - grounding.figures_in("\n".join(found)))
    if missing:
        return UNSUPPORTED, f"The figure {', '.join(missing)} is not in the cited source."
    return None


_WORD_TERM = r"[A-Z][\w+-]*(?:\.\d+)*"
_TERM = re.compile(rf"{_WORD_TERM}(?:\s+(?:{_WORD_TERM}|\d[\w+-]*(?:\.\d+)*))*")


def named_terms(text: str) -> list[str]:
    """Names and acronyms in a claim (Azure Key Vault, ISO 27001, SOC 2), to be looked for in its evidence."""
    terms: list[str] = []
    for match in _TERM.finditer(text):
        term = match.group().strip()
        single = " " not in term
        if single and not (re.search(r"[A-Z].*[A-Z]", term) or re.search(r"\d", term)):
            continue  # an ordinary capitalised word
        terms.append(term)
    return list(dict.fromkeys(terms))


def missing_terms(claim: dict[str, Any], passages: dict[str, str | None], requirement_text: str) -> list[str]:
    haystack = " ".join([requirement_text, *(p for i in claim["evidence_ids"] if (p := passages.get(i)))]).lower()
    return [t for t in named_terms(claim["text"]) if t.lower() not in haystack][:5]


# ── The verifier ─────────────────────────────────────────


def verifier_prompt(
    claims: list[dict[str, Any]], passages: dict[str, str | None], hints: dict[str, list[str]]
) -> str:
    cited = list(dict.fromkeys(i for c in claims for i in c["evidence_ids"] if passages.get(i)))
    evidence = "\n".join(fence(f"EVIDENCE-{i}", str(passages[i])[:MAX_PASSAGE_CHARS]) for i in cited)
    listed = "\n".join(f"{c['id']} (cites {', '.join(c['evidence_ids'])}): {c['text']}" for c in claims)
    notes = "".join(
        f"- {cid}: these names are not in its cited passages: {', '.join(names)}\n" for cid, names in hints.items() if names
    )
    return (
        "Verify each claim against the evidence passages it cites. Judge only on those passages.\n\n"
        f"## Evidence passages\n{evidence}\n\n"
        f"## Claims\n{fence('CLAIMS', listed)}\n\n"
        + (f"## Notes\n{notes}\n" if notes else "")
        + 'Return ONLY the JSON object {"verdicts": [{"id", "verdict", "reason"}]}, one entry for every claim id.'
    )


def _read_verdicts(text: str) -> dict[str, tuple[str, str]]:
    """``{claim id: (verdict, reason)}`` from the verifier's reply; a verdict it does not know is dropped."""
    reply = runner.parse_json_object(runner.post_generation_guard(text)[0])
    verdicts: dict[str, tuple[str, str]] = {}
    for item in reply.get("verdicts") if isinstance(reply.get("verdicts"), list) else []:
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict", "")).strip().lower()
        if verdict in VERDICTS:
            verdicts[str(item.get("id", "")).strip()] = (verdict, " ".join(str(item.get("reason") or "").split())[:MAX_REASON_CHARS])
    return verdicts


def _ask_verifier(prompt: str) -> tuple[dict[str, tuple[str, str]], str]:
    """The verifier's verdicts, or ``({}, why not)``. A verifier that fails leaves claims unchecked; it never fails a draft."""
    try:
        agent_id = lifecycle.ensure_agent(lifecycle.VERIFIER_AGENT_NAME)
        result = runner.run_agent_prompt(agent_id, prompt)
    except Exception as exc:
        logger.error("The verifier could not be run: %s", exc)
        return {}, "The verifier could not be run."
    if not result.completed:
        logger.error("The verifier run ended with status %s", result.status)
        return {}, f"The verifier did not finish ({result.status})."
    try:
        return _read_verdicts(result.text), ""
    except (ValueError, IndexError):
        logger.warning("The verifier did not return a usable reply")
        return {}, "The verifier's reply could not be read."


# ── Putting it together ──────────────────────────────────


def summarise(claims: list[dict[str, Any]], *, model: str = "", reason: str = "") -> dict[str, Any]:
    """The overall verification of an answer from its claims."""
    counts = {v: sum(1 for c in claims if c.get("verdict") == v) for v in VERDICTS}
    unchecked = sum(1 for c in claims if not c.get("verdict"))
    if counts[UNSUPPORTED] or counts[CONTRADICTED]:
        status = FLAGGED
    elif claims and counts[SUPPORTED] == len(claims):
        status = VERIFIED
    elif counts[SUPPORTED] or counts[PARTIAL]:
        status = PARTIALLY_VERIFIED
    else:
        status = UNCHECKED
    summary: dict[str, Any] = {
        "status": status,
        "claims": len(claims),
        **counts,
        "unchecked": unchecked,
        "by_rule": sum(1 for c in claims if c.get("checked_by") == "rule"),
    }
    if model:
        summary["model"] = model
    if status == UNCHECKED and reason:
        summary["reason"] = reason
    return summary


def check(
    answer: dict[str, Any], *, requirement_text: str, docs: list[dict[str, Any]], web_grounded: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """``(evidence, claims, verification)`` for an answer: every claim with its verdict. Changes nothing else."""
    settings = get_settings()
    evidence = assign_evidence_ids(answer.get("evidence"))
    claims = read_claims(answer.get("claims"), {e["id"] for e in evidence})
    model = lifecycle.SPECS[lifecycle.VERIFIER_AGENT_NAME].model(settings)

    passages: dict[str, str | None] = {}
    external: set[str] = set()
    for entry in evidence:
        passage = states.locate_passage(entry, docs)
        passages[entry["id"]] = passage
        if passage is None and web_grounded and not _in_docs(entry, docs):
            external.add(entry["id"])  # a web source: its page is not visible to us
            entry["quote_verified"] = None
        else:
            entry["quote_verified"] = passage is not None

    if not claims:
        return evidence, claims, summarise(claims, reason="The writer listed no claims to check.")
    if not settings.verify_answers:
        return evidence, claims, summarise(claims, reason="Verification is turned off.")

    with spans.stage("verify", claims=len(claims)) as span:
        pending = []
        for claim in claims:
            decided = rule_verdict(claim, passages, external)
            if decided is None:
                pending.append(claim)
                continue
            claim["verdict"], claim["reason"] = decided
            claim["checked_by"] = "rule"

        failure = ""
        if pending:
            hints = {c["id"]: missing_terms(c, passages, requirement_text) for c in pending}
            verdicts, failure = _ask_verifier(verifier_prompt(pending, passages, hints))
            for claim in pending:
                claim["checked_by"] = "model"
                if claim["id"] in verdicts:
                    claim["verdict"], claim["reason"] = verdicts[claim["id"]]
                else:
                    claim["verdict"], claim["reason"] = None, failure or "The verifier gave no verdict for this claim."

        summary = summarise(claims, model=model if pending else "", reason=failure)
        span.set(
            claims__flagged=summary[UNSUPPORTED] + summary[CONTRADICTED],
            claims__by_rule=summary["by_rule"],
            verification__status=summary["status"],
            verifier__model=model,
        )
    return evidence, claims, summary


def _in_docs(entry: dict[str, Any], docs: list[dict[str, Any]]) -> bool:
    url = str(entry.get("source_url", ""))
    return any(kb_search._is_allowed(url, {kb_search._normalize_url(str(d.get("source_url", "")))}) for d in docs)


def verify_answer(
    answer: dict[str, Any], requirement: dict[str, Any], *, docs: list[dict[str, Any]], web_grounded: bool = False
) -> dict[str, Any]:
    """The answer with its claims checked, its state adjusted and what is wrong listed under its risks.

    Placeholders and failures have nothing to verify and are returned as they are.
    """
    if answer.get("state") not in states.ANSWERED:
        return answer
    evidence, claims, verification = check(
        answer, requirement_text=str(requirement.get("text", "")), docs=docs, web_grounded=web_grounded
    )
    result = {**answer, "evidence": evidence, "claims": claims, "verification": verification}
    flagged = [c for c in claims if c.get("verdict") in FLAGGED_VERDICTS]
    if flagged:
        result["risks"] = [
            *answer.get("risks", []),
            *(f"Unverified claim ({c['verdict']}): {c['text']} {c.get('reason', '')}".strip()[:400] for c in flagged[:MAX_RISKS]),
        ]
        contradicted = [c for c in flagged if c["verdict"] == CONTRADICTED]
        if contradicted and result["state"] in (states.FULLY, states.PARTIAL):
            result["state"] = states.CONFLICTING
            result["missing"] = result.get("missing") or states.clean_missing(
                f"A claim is contradicted by its source: {contradicted[0]['text']}"
            )
        elif result["state"] == states.FULLY:
            result["state"] = states.PARTIAL
            result["missing"] = result.get("missing") or states.clean_missing(
                f"{len(flagged)} claim(s) are not supported by the cited sources"
            )
    return result
