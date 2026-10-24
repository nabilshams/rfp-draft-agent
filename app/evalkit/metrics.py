"""Scoring an evaluation run: extraction, retrieval, citations, unsupported claims, abstention, compliance."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from statistics import mean
from typing import Any

from app.agents import states, verify
from app.agents.grounding import figures_in

# Metrics where a bigger number is better, and where a smaller one is. Used for regression checks.
HIGHER_IS_BETTER = (
    "extraction_recall",
    "extraction_precision",
    "extraction_recall_pdf", "extraction_precision_pdf", "extraction_recall_docx", "extraction_precision_docx",
    "extraction_recall_xlsx", "extraction_precision_xlsx",
    "retrieval_hit_rate",
    "retrieval_mrr",
    "citation_support_rate",
    "abstention_correctness",
    "answer_correctness",
    "compliance_correctness",
    "answer_rate",
    "state_correctness",
    "claim_support_rate",
    "typed_field_accuracy",
    "duplicate_grouping_correctness",
    "non_answered_capture",
    "deadline_capture",
    "confidence_separation",
    "injected_confidence_separation",
    "injection_recall",
    "deterministic_catch_rate",
)
LOWER_IS_BETTER = (
    "unsupported_claim_rate_raw", "unsupported_claim_rate_final", "false_answer_rate", "injection_false_positive_rate"
)

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by for from has have in is it its of on or shall must should the to will with your our "
    "vendor provider contractor".split()
)


def words(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def squash(text: str) -> str:
    """Lower-cased text with whitespace collapsed, for substring checks."""
    return " ".join((text or "").lower().split())


# ── Extraction ───────────────────────────────────────────


def _overlap(extracted: str, expected: str) -> float:
    """How much of the shorter text is in the longer one (by content words); 1.0 means one contains the other."""
    a = {w for w in words(extracted) if w not in _STOP}
    b = {w for w in words(expected) if w not in _STOP}
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def match_requirements(
    extracted: list[dict[str, Any]], expected: list[dict[str, Any]], threshold: float = 0.8
) -> dict[str, int]:
    """One-to-one matching of expected requirement ids to positions in *extracted*, best matches first."""
    pairs = sorted(
        (
            # Equal wording (a repeated question) is told apart by the id the document gave it
            (_overlap(e.get("text", ""), x["text"]), e.get("id") == x["id"], -i, x["id"], i)
            for x in expected
            for i, e in enumerate(extracted)
        ),
        reverse=True,
    )
    matched: dict[str, int] = {}
    taken: set[int] = set()
    for score, _, _, expected_id, index in pairs:
        if score < threshold:
            break
        if expected_id in matched or index in taken:
            continue
        matched[expected_id] = index
        taken.add(index)
    return matched


# ── Per-requirement scoring ──────────────────────────────


@dataclass
class RequirementScore:
    case: str
    id: str
    text: str
    supported: bool  # the knowledge base has what is needed to answer
    extracted: bool = False
    retrieval_expected: bool = False
    retrieval_rank: int | None = None
    answered: bool = False  # a usable answer came back (the model run completed)
    abstained: bool = False
    meets: str = ""
    abstention_ok: bool | None = None
    state: str = ""
    verification: str = ""  # the claim verifier's overall status for the answer
    confidence: str = ""  # the band: high, medium or low
    confidence_reasons: list[str] = field(default_factory=list)
    typed_ok: bool | None = None  # kind, response type and evidence flag as labelled (None when the case labels none)
    duplicate_ok: bool | None = None  # grouped with the right earlier item, or correctly not grouped
    needs_edit: bool | None = None  # a person would have to change this answer (judged without the confidence inputs)
    claims: dict[str, int] = field(default_factory=dict)  # verdict -> number of claims
    state_ok: bool | None = None  # the answer state is one the requirement should end in
    answer_ok: bool | None = None  # contains_any / forbid satisfied (None when the case sets none)
    compliance_ok: bool | None = None  # meets_in satisfied (None when the case sets none)
    citations: int = 0
    citations_supported: int = 0
    unsupported_raw: list[str] = field(default_factory=list)
    unsupported_final: list[str] = field(default_factory=list)
    latency_s: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    problems: list[str] = field(default_factory=list)


_ABSTAIN_PHRASES = ("not provided in sources", "recommend human review", "no relevant", "unable to confirm")


def is_abstention(answer: dict[str, Any]) -> bool:
    """Whether an answer declines to claim the capability: is a placeholder, says so, or says No with nothing to cite."""
    if answer.get("state") in states.PLACEHOLDER_STATES:
        return True
    text = squash(answer.get("answer_markdown", ""))
    if any(phrase in text for phrase in _ABSTAIN_PHRASES):
        return True
    return str(answer.get("meets", "")).lower() == "no" and not answer.get("evidence")


def count_citations(answer: dict[str, Any], docs: list[dict[str, Any]]) -> tuple[int, int]:
    """(evidence entries, entries whose quote really is in the retrieved document they cite)."""
    return states.count_supported_citations(answer, docs)


def check_answer_text(text: str, contains_any: list[list[str]], forbid: list[str]) -> list[str]:
    """What is wrong with *text*: a required group with none of its phrases, or a forbidden phrase present."""
    lowered = squash(text)
    problems = []
    for group in contains_any:
        if not any(squash(phrase) in lowered for phrase in group):
            problems.append(f"missing one of: {', '.join(group)}")
    for phrase in forbid:
        if squash(phrase) in lowered:
            problems.append(f"contains forbidden: {phrase}")
    return problems


def unsupported_figures(text: str, sources: str) -> list[str]:
    return sorted(figures_in(text) - figures_in(sources))


def typed_check(expected: dict[str, Any], got: dict[str, Any] | None, expected_id_of: dict[str, str]) -> dict[str, Any]:
    """How the extracted item's typed fields compare with the labels. *expected_id_of* maps an extracted id to its golden id."""
    if got is None:
        return {}
    wrong = [
        f"{key} {got.get(key)!r}, expected {expected[key]!r}"
        for key in ("kind", "response_type", "evidence_required")
        if key in expected and got.get(key) != expected[key]
    ]
    grouped_with = expected_id_of.get(got.get("duplicate_of", ""), "")
    duplicate_ok = grouped_with == expected.get("duplicate_of", "")
    if not duplicate_ok:
        wrong.append(f"grouped with {grouped_with or 'nothing'}, expected {expected.get('duplicate_of') or 'nothing'}")
    labelled = any(key in expected for key in ("kind", "response_type", "evidence_required"))
    return {"typed_ok": (not [w for w in wrong if not w.startswith("grouped")]) if labelled else None,
            "duplicate_ok": duplicate_ok, "problems": wrong}


def score_requirement(
    *,
    case: str,
    expected: dict[str, Any],
    extracted: bool,
    docs: list[dict[str, Any]],
    answer: dict[str, Any] | None,
    raw_text: str,
    sources: str,
    retrieval_rank: int | None,
    latency_s: float | None,
    usage: dict[str, int] | None,
    typed: dict[str, Any] | None = None,
) -> RequirementScore:
    score = RequirementScore(
        case=case,
        id=expected["id"],
        text=expected["text"],
        supported=bool(expected.get("supported", True)),
        extracted=extracted,
        retrieval_expected=bool(expected.get("expected_sources")),
        retrieval_rank=retrieval_rank,
        latency_s=latency_s,
        input_tokens=(usage or {}).get("input_tokens"),
        output_tokens=(usage or {}).get("output_tokens"),
    )
    typed = typed or {}
    score.typed_ok, score.duplicate_ok = typed.get("typed_ok"), typed.get("duplicate_ok")
    score.problems.extend(f"extraction: {w}" for w in typed.get("problems", []))
    if score.retrieval_expected and retrieval_rank is None:
        score.problems.append("expected source not retrieved")
    if not extracted:
        score.problems.append("requirement was not extracted")
    if answer is None or answer.get("error") or answer.get("parse_error"):
        score.problems.append("no usable answer")
        return score

    score.answered = True
    score.meets = str(answer.get("meets", ""))
    score.state = str(answer.get("state", ""))
    summary = answer.get("verification") or {}
    score.verification = str(summary.get("status", ""))
    score.claims = {v: int(summary.get(v, 0)) for v in verify.VERDICTS}
    band = answer.get("confidence") or {}
    score.confidence = str(band.get("band", ""))
    score.confidence_reasons = [r["text"] for r in band.get("reasons", []) if r.get("effect") == "lowers"]
    if score.claims[verify.UNSUPPORTED] + score.claims[verify.CONTRADICTED]:
        flagged = [c for c in answer.get("claims", []) if c.get("verdict") in verify.FLAGGED_VERDICTS]
        score.problems.append(
            f"{len(flagged)} claim(s) the evidence does not support: " + "; ".join(c["text"][:70] for c in flagged[:2])
        )
    wanted = expected.get("state_in") or (states.ANSWERED if score.supported else states.PLACEHOLDER_STATES)
    score.state_ok = score.state in wanted
    if not score.state_ok:
        score.problems.append(f"answer state {score.state or 'none'!r} not in {list(wanted)}")
    score.abstained = is_abstention(answer)
    score.abstention_ok = score.abstained != score.supported
    if not score.abstention_ok:
        score.problems.append("answered an unsupported requirement" if not score.supported else "declined a supported one")

    text_problems = check_answer_text(
        answer.get("answer_markdown", ""), expected.get("contains_any", []), expected.get("forbid", [])
    )
    if expected.get("contains_any") or expected.get("forbid"):
        score.answer_ok = not text_problems
        score.problems.extend(text_problems)
    if expected.get("meets_in"):
        score.compliance_ok = score.meets in expected["meets_in"]
        if not score.compliance_ok:
            score.problems.append(f"compliance {score.meets!r} not in {expected['meets_in']}")

    score.citations, score.citations_supported = count_citations(answer, docs)
    if score.citations != score.citations_supported:
        score.problems.append(f"{score.citations - score.citations_supported} citation(s) not supported by the source")
    score.unsupported_raw = unsupported_figures(raw_text, sources)
    score.unsupported_final = unsupported_figures(answer.get("answer_markdown", ""), sources)
    if score.unsupported_raw:
        score.problems.append(f"model wrote figures not in the sources: {', '.join(score.unsupported_raw)}")
    # Whether a person would have to change this answer, judged only on things the confidence band is not made from
    # (so not on the claim verdicts, the figures or the citations): the expected facts, the expected compliance, and
    # answering what it should not have.
    score.needs_edit = score.answer_ok is False or score.compliance_ok is False or score.state_ok is False
    return score


# ── Aggregation ──────────────────────────────────────────


def _rate(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def summarise(scores: list[RequirementScore], expected_total: int, extracted_total: int, matched_total: int) -> dict[str, Any]:
    """The headline metrics. A metric with nothing to measure is ``None`` (and skipped by regression checks)."""
    answered = [s for s in scores if s.answered]
    cited = sum(s.citations for s in scores)
    retrieval = [s for s in scores if s.retrieval_expected]
    checked = [s for s in scores if s.answer_ok is not None]
    compliance = [s for s in scores if s.compliance_ok is not None]
    unsupported = [s for s in scores if not s.supported]
    latencies = sorted(s.latency_s for s in scores if s.latency_s is not None)
    in_tokens = [s.input_tokens for s in scores if s.input_tokens is not None]
    out_tokens = [s.output_tokens for s in scores if s.output_tokens is not None]

    return {
        "requirements_expected": expected_total,
        "requirements_extracted": extracted_total,
        "extraction_recall": _rate(matched_total, expected_total),
        "extraction_precision": _rate(matched_total, extracted_total),
        "retrieval_hit_rate": _rate(sum(1 for s in retrieval if s.retrieval_rank), len(retrieval)),
        "retrieval_mrr": round(mean(1 / s.retrieval_rank if s.retrieval_rank else 0.0 for s in retrieval), 4)
        if retrieval
        else None,
        "typed_field_accuracy": _rate(sum(1 for s in scores if s.typed_ok), sum(1 for s in scores if s.typed_ok is not None)),
        "duplicate_grouping_correctness": _rate(
            sum(1 for s in scores if s.duplicate_ok), sum(1 for s in scores if s.duplicate_ok is not None)
        ),
        "answer_rate": _rate(len(answered), len(scores)),
        "claim_support_rate": _rate(
            sum(s.claims.get(verify.SUPPORTED, 0) for s in answered),
            sum(sum(s.claims.values()) for s in answered),
        ),
        **confidence_metrics(answered),
        "claims_per_answer": round(mean(sum(s.claims.values()) for s in answered), 2) if answered else None,
        "citation_support_rate": _rate(sum(s.citations_supported for s in scores), cited),
        "unsupported_claim_rate_raw": _rate(sum(1 for s in answered if s.unsupported_raw), len(answered)),
        "unsupported_claim_rate_final": _rate(sum(1 for s in answered if s.unsupported_final), len(answered)),
        "abstention_correctness": _rate(sum(1 for s in answered if s.abstention_ok), len(answered)),
        "state_correctness": _rate(sum(1 for s in answered if s.state_ok), len(answered)),
        "false_answer_rate": _rate(sum(1 for s in unsupported if s.answered and not s.abstained), len(unsupported)),
        "answer_correctness": _rate(sum(1 for s in checked if s.answer_ok), len(checked)),
        "compliance_correctness": _rate(sum(1 for s in compliance if s.compliance_ok), len(compliance)),
        "latency_mean_s": round(mean(latencies), 2) if latencies else None,
        "latency_p95_s": round(latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))], 2) if latencies else None,
        "input_tokens_per_requirement": round(mean(in_tokens)) if in_tokens else None,
        "output_tokens_per_requirement": round(mean(out_tokens)) if out_tokens else None,
    }


def estimate_cost(metrics: dict[str, Any], input_price: float | None, output_price: float | None) -> float | None:
    """Dollars per requirement from average tokens and prices per million tokens (``None`` without both)."""
    tokens_in, tokens_out = metrics.get("input_tokens_per_requirement"), metrics.get("output_tokens_per_requirement")
    if input_price is None or output_price is None or tokens_in is None or tokens_out is None:
        return None
    return round((tokens_in * input_price + tokens_out * output_price) / 1_000_000, 5)


def confidence_metrics(answered: list[RequirementScore]) -> dict[str, Any]:
    """How the confidence bands line up with what a person would have to change.

    Only answers that are answers count: a placeholder is Low by definition and always needs a person, which would make
    any band look right. ``confidence_separation`` is the edit rate of the Low band minus the High band's (positive =
    the band orders answers the right way); it is ``None`` unless both bands have answers.
    """
    judged = [s for s in answered if s.state in states.ANSWERED and s.needs_edit is not None]
    out: dict[str, Any] = {}
    rates: dict[str, float | None] = {}
    for band in ("high", "medium", "low"):
        members = [s for s in judged if s.confidence == band]
        rates[band] = _rate(sum(1 for s in members if s.needs_edit), len(members))
        out[f"confidence_{band}_answers"] = len(members)
        out[f"confidence_{band}_edit_rate"] = rates[band]
    low, high = rates["low"], rates["high"]
    out["confidence_separation"] = round(low - high, 4) if low is not None and high is not None else None
    return out


def verification_cost(records: list[dict[str, Any]]) -> dict[str, Any]:
    """What one verifier call costs on the golden answers: latency and tokens (from the live recording)."""
    latencies = [r["latency_s"] for r in records if r.get("latency_s") is not None]
    in_tokens = [r["usage"]["input_tokens"] for r in records if r.get("usage", {}).get("input_tokens")]
    out_tokens = [r["usage"]["output_tokens"] for r in records if r.get("usage", {}).get("output_tokens")]
    return {
        "verifier_calls": len(records),
        "verification_latency_mean_s": round(mean(latencies), 2) if latencies else None,
        "verification_input_tokens_per_call": round(mean(in_tokens)) if in_tokens else None,
        "verification_output_tokens_per_call": round(mean(out_tokens)) if out_tokens else None,
    }


# ── Regression check ─────────────────────────────────────


def compare(metrics: dict[str, Any], baseline: dict[str, Any], tolerance: float = 0.02) -> list[str]:
    """The metrics that got worse than *baseline* by more than *tolerance* (absolute), as readable lines."""
    regressions = []
    for name in HIGHER_IS_BETTER + LOWER_IS_BETTER:
        now, before = metrics.get(name), baseline.get(name)
        if now is None and before is not None:
            regressions.append(f"{name}: no longer measured (baseline {before})")
            continue
        if now is None or before is None:
            continue
        worse = (before - now) if name in HIGHER_IS_BETTER else (now - before)
        if worse > tolerance:
            regressions.append(f"{name}: {before} -> {now} (worse by {round(worse, 4)}, tolerance {tolerance})")
    return regressions
