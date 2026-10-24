"""An explainable confidence band for an answer: High, Medium or Low, with the reasons that produced it.

There is no percentage, and no number the model reports is an input. Every input is a fact the system can check:

* how many independent sources have a quote that is really in the document they cite
* how authoritative they are, and whether any is superseded or still a draft
* how old they are
  (each of these is judged claim by claim: an answer is only as strong as its weakest claim)
* how well the cited sources matched the requirement (their rank and score relative to the best match)
* the fraction of the answer's claims the verifier found supported (see :mod:`app.agents.verify`)
* whether the sources conflict, and whether part of the requirement is not covered

Each rule that applies adds a *reason* that lowers the band (to Medium or to Low) or supports it. The band is the lowest
any reason allows, so it is predictable: remove a source, or change one input, and the reasons that follow from it
change with it. Thresholds are configuration (``CONFIDENCE_*``). Similarity to an approved answer joins the inputs when
the approved-answer library exists (#38); until then an ``approved_answer`` source counts through its source type.

Placeholders and failures are Low: there is no answer to trust. Text a person wrote has no band: nothing machine-made
is being weighed.

The bands are calibrated against what people did with the answers, see ``calibration`` in :mod:`app.ledger.service` and
the evaluation's ``confidence_*`` metrics.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from app.agents import kb_search, states, verify
from app.config import Settings, get_settings

HIGH, MEDIUM, LOW = "high", "medium", "low"
BANDS = (LOW, MEDIUM, HIGH)  # worst first
LABELS = {HIGH: "High", MEDIUM: "Medium", LOW: "Low"}
_ORDER = {LOW: 0, MEDIUM: 1, HIGH: 2}

# Better source types first; an unknown or missing type ranks with historical material
_TIER = {"authoritative": 3, "approved_answer": 2, "historical": 1, "external": 0}
_WEAK_STATUSES = ("superseded", "draft")  # a source in one of these is not relied on as current

MAX_REASONS = 10


def _now_date() -> date:
    """Today. A function so the evaluation can pin it: source ages must not drift under a committed baseline."""
    return datetime.now(UTC).date()


def _reason(code: str, effect: str, text: str, level: str = HIGH) -> dict[str, str]:
    """``effect`` is ``lowers`` (the reason caps the band at *level*) or ``supports`` (it argues for the band)."""
    return {"code": code, "effect": effect, "level": level, "text": text}


def _age_days(effective: str, today: date) -> int | None:
    try:
        return (today - datetime.fromisoformat(effective[:10]).date()).days
    except ValueError:
        return None


def _years(days: int) -> str:
    years = days / 365
    return f"{years:.0f} years" if years >= 1.5 else f"{days // 30} months"


def _best_cited(evidence: list[dict[str, Any]], docs: list[dict[str, Any]]) -> tuple[int | None, float | None]:
    """The rank (1 = best match) and the score relative to the best match of the best-ranked document that is cited."""
    scored = [float(d.get("rank_score") or d.get("score") or 0.0) for d in docs]
    top = max(scored, default=0.0)
    for position, doc in enumerate(docs, start=1):
        known = {kb_search._normalize_url(str(doc.get("source_url", "")))}
        if any(kb_search._is_allowed(str(e.get("source_url", "")), known) for e in evidence):
            return position, (scored[position - 1] / top if top > 0 else None)
    return None, None


def is_strong(evidence: dict[str, Any]) -> bool:
    """A source to rely on alone: authoritative, or a reviewed past answer that is approved (never superseded or draft)."""
    if evidence.get("approval_status") in _WEAK_STATUSES:
        return False
    kind = str(evidence.get("source_type") or "")
    return kind == "authoritative" or (kind == "approved_answer" and evidence.get("approval_status") == "approved")


def _tier(evidence: dict[str, Any]) -> int:
    """The source type's rank, with a superseded or draft source counting as the weakest."""
    if evidence.get("approval_status") in _WEAK_STATUSES:
        return 0
    return _TIER.get(str(evidence.get("source_type") or ""), 1)


def _claim_groups(answer: dict[str, Any], found: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The found evidence each claim rests on. An answer is only as strong as its weakest claim, so the band looks at
    them one by one. With no claims listed (verification off) the whole answer is one group."""
    by_id = {str(e.get("id")): e for e in found if e.get("id")}
    groups = []
    for claim in answer.get("claims") or []:
        if isinstance(claim, dict):
            group = [by_id[i] for i in claim.get("evidence_ids") or [] if i in by_id]
            if group:
                groups.append(group)
    return groups or ([found] if found else [])


def features(
    answer: dict[str, Any], docs: list[dict[str, Any]], settings: Settings, today: date
) -> dict[str, Any]:
    """The facts the band is made from, so they can be shown, stored and tested."""
    evidence = [e for e in answer.get("evidence") or [] if isinstance(e, dict)]
    found = [e for e in evidence if e.get("quote_verified") is True]
    sources = {kb_search._normalize_url(str(e.get("source_url", ""))) for e in found}
    groups = _claim_groups(answer, found)

    def urls(group: list[dict[str, Any]]) -> int:
        return len({kb_search._normalize_url(str(e.get("source_url", ""))) for e in group})

    def newest(group: list[dict[str, Any]]) -> int | None:
        ages = [a for e in group if (a := _age_days(str(e.get("effective_date") or ""), today)) is not None]
        return min(ages) if ages else None

    claim_ages = [newest(g) for g in groups]
    known_ages = [a for a in claim_ages if a is not None]
    weak = [g for g in groups if max(_tier(e) for e in g) <= _TIER["historical"]]
    alone = [g for g in groups if g not in weak and urls(g) == 1 and not any(is_strong(e) for e in g)]
    summary = answer.get("verification") if isinstance(answer.get("verification"), dict) else {}
    claims = int(summary.get("claims", 0) or 0)
    # The share is of the claims that were judged: a claim nobody checked (verification off, a web source, a verifier
    # outage) is not an unsupported one, and is covered by the "unchecked" rule instead
    checked = claims - int(summary.get("unchecked", 0) or 0)
    supported = int(summary.get(verify.SUPPORTED, 0) or 0)
    rank, relative = _best_cited(found, docs)
    return {
        "sources": len(sources),
        "unfound_quotes": sum(1 for e in evidence if e.get("quote_verified") is False),
        "unverifiable_sources": sum(1 for e in evidence if e.get("quote_verified") is None),
        "authoritative": any(e.get("source_type") == "authoritative" and e.get("approval_status") not in _WEAK_STATUSES for e in found),
        "reviewed_answer": any(is_strong(e) and e.get("source_type") == "approved_answer" for e in found),
        "claims_on_weak_sources": len(weak),
        "claims_on_one_source": len(alone),
        "corroborated_claims": sum(1 for g in groups if urls(g) >= 2),
        "oldest_claim_age_days": max(known_ages) if known_ages else None,
        "undated_claims": len(claim_ages) - len(known_ages),
        "verification": summary.get("status", ""),
        "claims": claims,
        "supported_fraction": round(supported / checked, 3) if checked > 0 else None,
        "unchecked_claims": claims - checked,
        "flagged_claims": int(summary.get(verify.UNSUPPORTED, 0) or 0) + int(summary.get(verify.CONTRADICTED, 0) or 0),
        "contradicted_claims": int(summary.get(verify.CONTRADICTED, 0) or 0),
        "conflict": answer.get("state") == states.CONFLICTING,
        "partial": answer.get("state") == states.PARTIAL,
        "best_cited_rank": rank,
        "best_cited_relative_score": round(relative, 3) if relative is not None else None,
    }


def _lowering(f: dict[str, Any], settings: Settings) -> list[dict[str, str]]:
    reasons: list[dict[str, str]] = []
    add = reasons.append
    if f["flagged_claims"]:
        add(_reason("claims_unsupported", "lowers",
                    f"{f['flagged_claims']} claim(s) are not supported by the evidence they cite", LOW))
    if f["conflict"] or f["contradicted_claims"]:
        add(_reason("conflict", "lowers", "The sources conflict, or a claim is contradicted by its source", LOW))
    if not f["sources"] and not f["unverifiable_sources"]:
        add(_reason("no_verified_source", "lowers", "No quoted passage was found in a source", LOW))
    elif not f["sources"]:
        add(_reason("web_only", "lowers", "Rests only on web sources, which are not checked here", MEDIUM))
    if f["unfound_quotes"]:
        add(_reason("quote_not_found", "lowers", f"{f['unfound_quotes']} cited quote(s) are not in the source", MEDIUM))
    if f["sources"]:
        support = f["supported_fraction"]
        if support is not None and support < settings.confidence_low_support:
            add(_reason("few_claims_supported", "lowers", f"Only {support:.0%} of the claims are supported", LOW))
        elif support is not None and support < settings.confidence_high_support and not f["flagged_claims"]:
            add(_reason("claims_partly_supported", "lowers", f"Only {support:.0%} of the claims are fully supported", MEDIUM))
        age = f["oldest_claim_age_days"]
        if age is not None and age > settings.confidence_stale_days:
            add(_reason("stale", "lowers", f"A claim rests only on sources {_years(age)} old", LOW))
        elif age is not None and age > settings.confidence_fresh_days:
            add(_reason("dated", "lowers", f"A claim rests only on sources {_years(age)} old", MEDIUM))
        if f["undated_claims"]:
            add(_reason("undated", "lowers", "The date of some sources is not known", MEDIUM))
        if f["claims_on_weak_sources"]:
            add(_reason("weak_sources", "lowers",
                        f"{f['claims_on_weak_sources']} claim(s) rest only on historical, external, superseded or draft material", MEDIUM))
        if f["claims_on_one_source"]:
            add(_reason("single_source", "lowers",
                        f"{f['claims_on_one_source']} claim(s) rest on one source that is not authoritative or approved", MEDIUM))
        rank = f["best_cited_rank"]
        if rank is not None and rank > settings.confidence_top_rank:
            add(_reason("weak_match", "lowers", f"The cited sources were not among the top {settings.confidence_top_rank} matches", MEDIUM))
        elif (
            f["best_cited_relative_score"] is not None
            and f["best_cited_relative_score"] < settings.confidence_min_relative_score
        ):
            add(_reason("weak_match", "lowers", "The cited sources matched the requirement much less well than the best match", MEDIUM))
    if f["partial"]:
        add(_reason("partial", "lowers", "Only part of the requirement is covered", MEDIUM))
    if f["verification"] in ("", verify.UNCHECKED):
        add(_reason("unchecked", "lowers", "The claims were not checked against the evidence", MEDIUM))
    elif f["unchecked_claims"]:
        add(_reason("partly_unchecked", "lowers", f"{f['unchecked_claims']} claim(s) could not be checked against the evidence", MEDIUM))
    return reasons


def _supporting(f: dict[str, Any], settings: Settings) -> list[dict[str, str]]:
    reasons: list[dict[str, str]] = []
    if f["sources"] >= 2:
        reasons.append(_reason("corroborated", "supports", f"{f['sources']} independent sources cited, with their quotes found"))
    elif f["sources"] == 1:
        reasons.append(_reason("one_source", "supports", "One source cited, with its quote found"))
    if f["authoritative"]:
        reasons.append(_reason("authoritative", "supports", "Backed by an authoritative source"))
    elif f["reviewed_answer"]:
        reasons.append(_reason("approved_answer", "supports", "Backed by a reviewed past answer"))
    age = f["oldest_claim_age_days"]
    if age is not None and age <= settings.confidence_fresh_days:
        reasons.append(_reason("fresh", "supports", f"Every claim rests on a source {_years(age)} old or newer"))
    if f["verification"] == verify.VERIFIED:
        reasons.append(_reason("verified", "supports", f"All {f['claims']} claim(s) are supported by their evidence"))
    rank = f["best_cited_rank"]
    if rank is not None and rank <= settings.confidence_top_rank:
        reasons.append(_reason("good_match", "supports", "The cited source is among the best matches"))
    return reasons

def assess(
    answer: dict[str, Any],
    docs: list[dict[str, Any]] | None = None,
    *,
    settings: Settings | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """``{"band", "reasons", "features"}`` for an answer. Reasons that lower the band come first, the worst first."""
    settings = settings or get_settings()
    today = today or _now_date()
    state = answer.get("state")
    f = features(answer, docs or [], settings, today)

    if state == states.FAILED:
        reasons = [_reason("failed", "lowers", "Drafting failed: there is no answer to trust", LOW)]
    elif state in states.PLACEHOLDER_STATES:
        reasons = [_reason("no_answer", "lowers", f"{states.LABELS[state]}: a person has to supply the answer", LOW)]
    else:
        lowering = _lowering(f, settings)
        # What lowers the band first, then what still speaks for the answer
        reasons = sorted(lowering, key=lambda r: _ORDER[r["level"]]) + _supporting(f, settings)
    band = min((r["level"] for r in reasons if r["effect"] == "lowers"), key=_ORDER.__getitem__, default=HIGH)
    return {"band": band, "reasons": reasons[:MAX_REASONS], "features": f}


def describe(confidence: Any, reasons: int = 3) -> str:
    """``Low: 2 claim(s) are not supported...; One source...`` for a document, or ``""`` when there is no band."""
    if not isinstance(confidence, dict) or confidence.get("band") not in LABELS:
        return ""
    lowering = [r["text"] for r in confidence.get("reasons") or [] if isinstance(r, dict) and r.get("effect") == "lowers"]
    shown = lowering or [r["text"] for r in confidence.get("reasons") or [] if isinstance(r, dict)]
    return LABELS[confidence["band"]] + (": " + "; ".join(shown[:reasons]) if shown else "")


def attach(answer: dict[str, Any], docs: list[dict[str, Any]]) -> dict[str, Any]:
    """The answer with its confidence."""
    return {**answer, "confidence": assess(answer, docs)}
