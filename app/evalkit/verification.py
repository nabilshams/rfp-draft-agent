"""Evaluating the claim verifier on claims that are known to be right or wrong.

``evals/golden/injections.json`` holds claims written against the golden knowledge base: some are correct (and must
be accepted), some are deliberately wrong (a fabricated quote, an invented capability, a changed number, an overstated
scope) and must be caught. Each is checked by the real verification code. Replay uses the recorded verifier replies,
so the result is the same every time and needs no Azure access; ``--mode live --record`` asks the real verifier.

* ``injection_recall``: the share of wrong claims that did not come out ``supported``
* ``injection_false_positive_rate``: the share of correct claims that did not come out ``supported``
* ``deterministic_catch_rate``: of the claims that the rules alone must catch (fabricated quotes, nothing cited, a
  figure that is not in the cited text), the share caught without any model call

The targets below are what the roadmap's acceptance criterion asks to be agreed; the committed baseline meets them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents import confidence, lifecycle, states, verify

TARGET_RECALL = 0.8
TARGET_FALSE_POSITIVE_RATE = 0.1
RULE_KINDS = ("fabricated_quote", "quote_from_another_document", "cites_nothing", "wrong_percentage")


@dataclass
class InjectionOutcome:
    id: str
    kind: str
    expect: str  # "wrong" or "right"
    claim: str
    verdict: str | None
    checked_by: str
    reason: str
    model_calls: int = 0
    ok: bool = False  # what was expected happened (a wrong claim was caught, a right one accepted)
    band: str = ""  # the confidence band an answer made of this one claim would get


@dataclass
class InjectionRun:
    metrics: dict[str, Any] = field(default_factory=dict)
    outcomes: list[InjectionOutcome] = field(default_factory=list)


def load_injections(golden_dir: Path) -> list[dict[str, Any]]:
    path = golden_dir / "injections.json"
    return json.loads(path.read_text(encoding="utf-8"))["cases"] if path.exists() else []


PROVENANCE = ("source_type", "authority", "effective_date", "version", "approval_status")


def _docs(kb: list[dict[str, Any]], blob_url: str) -> list[dict[str, Any]]:
    return [
        {
            "title": d["title"], "source_url": f"{blob_url}/{d['title']}", "content": chunk["text"],
            **{k: d[k] for k in PROVENANCE if d.get(k)},
        }
        for d in kb
        for chunk in d["chunks"]
    ]


def _answer(case: dict[str, Any], blob_url: str, provenance: dict[str, dict[str, Any]]) -> dict[str, Any]:
    evidence = [
        {"id": f"E{i}", "title": e["doc"], "source_url": f"{blob_url}/{e['doc']}", "quote": e["quote"],
         **{k: v for k, v in provenance.get(e["doc"], {}).items()}}
        for i, e in enumerate(case.get("evidence", []), start=1)
    ]
    ids = [e["id"] for e in evidence] if case.get("cites", True) else []
    return {"evidence": evidence, "claims": [{"text": case["claim"], "evidence_ids": ids}]}


def run_injections(
    cases: list[dict[str, Any]],
    kb: list[dict[str, Any]],
    blob_url: str,
    model: Any,
) -> InjectionRun:
    """Check every injected claim. *model* is the evaluation's model side (``key``, ``verifier_calls``)."""
    docs = _docs(kb, blob_url)
    provenance = {d["title"]: {k: d[k] for k in PROVENANCE if d.get(k)} for d in kb}
    outcomes: list[InjectionOutcome] = []
    for case in cases:
        model.key = case["id"]
        calls_before = model.verifier_calls
        evidence, claims, overall = verify.check(
            _answer(case, blob_url, provenance), requirement_text=case["requirement"], docs=docs
        )
        claim = claims[0] if claims else {}
        verdict = claim.get("verdict")
        wrong = case["expect"] == "wrong"
        accepted = verdict == verify.SUPPORTED
        outcomes.append(
            InjectionOutcome(
                id=case["id"],
                kind=case["kind"],
                expect=case["expect"],
                claim=case["claim"],
                verdict=verdict,
                checked_by=claim.get("checked_by", ""),
                reason=claim.get("reason", ""),
                model_calls=model.verifier_calls - calls_before,
                ok=(not accepted) if wrong else accepted,
                band=_band(evidence, claims, overall, case, docs),
            )
        )
    return InjectionRun(metrics=summarise(outcomes), outcomes=outcomes)


def _band(
    evidence: list[dict[str, Any]], claims: list[dict[str, Any]], overall: dict[str, Any], case: dict[str, Any],
    docs: list[dict[str, Any]],
) -> str:
    """The band an answer made of this one claim would get. The cited documents are put first, so the retrieval inputs
    are neutral here: what is being tested is the band's response to the sources and the verifier's verdict."""
    cited = {e["source_url"] for e in evidence}
    ranked = sorted(docs, key=lambda d: d["source_url"] not in cited)
    answer = {"state": states.FULLY, "evidence": evidence, "claims": claims, "verification": overall}
    return confidence.assess(answer, ranked)["band"]


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def summarise(outcomes: list[InjectionOutcome]) -> dict[str, Any]:
    wrong = [o for o in outcomes if o.expect == "wrong"]
    right = [o for o in outcomes if o.expect == "right"]
    rules = [o for o in wrong if o.kind in RULE_KINDS]
    bands = {b: [o for o in outcomes if o.band == b] for b in ("high", "medium", "low")}
    rates = {b: _rate(sum(1 for o in members if o.expect == "wrong"), len(members)) for b, members in bands.items()}
    return {
        # The band on answers whose truth was decided in advance: wrong claims "need a change", right ones do not
        "injected_high_band_answers": len(bands["high"]),
        "injected_low_band_answers": len(bands["low"]),
        "injected_high_band_wrong_rate": rates["high"],
        "injected_low_band_wrong_rate": rates["low"],
        "injected_confidence_separation": round(rates["low"] - rates["high"], 4)
        if rates["low"] is not None and rates["high"] is not None else None,
        "injection_recall": _rate(sum(1 for o in wrong if o.ok), len(wrong)),
        "injection_false_positive_rate": _rate(sum(1 for o in right if not o.ok), len(right)),
        "deterministic_catch_rate": _rate(sum(1 for o in rules if o.ok and o.checked_by == "rule" and not o.model_calls), len(rules)),
    }


def replay_agent_id(name: str) -> str:
    return f"golden-{name}"


def is_verifier(agent_id: str) -> bool:
    """Whether an agent id is the verifier's (the golden replay id, or the live agent's)."""
    return agent_id in (replay_agent_id(lifecycle.VERIFIER_AGENT_NAME), lifecycle._agent_ids.get(lifecycle.VERIFIER_AGENT_NAME))
