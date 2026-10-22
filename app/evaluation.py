"""Offline retrieval evaluation: do the knowledge-base documents we expect come back?

A case is a query plus the sources that should be retrieved for it. A source is identified by a
substring of the document's title or URL (case-insensitive), so cases survive re-ingestion.

Metrics, over the top *k* results of each case:

* **hit rate** – share of cases where at least one expected source was retrieved
* **recall@k** – average share of each case's expected sources that were retrieved
* **MRR** – mean reciprocal rank of the first expected source (0 when none was retrieved)
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvalCase:
    id: str
    query: str
    expected_sources: tuple[str, ...]


@dataclass
class CaseResult:
    case: EvalCase
    retrieved: list[str]  # "title | url" of each result, best first
    found: list[str] = field(default_factory=list)  # expected sources that were retrieved
    first_rank: int | None = None  # 1-based rank of the first expected source

    @property
    def hit(self) -> bool:
        return self.first_rank is not None


@dataclass
class EvalReport:
    k: int
    results: list[CaseResult]

    @property
    def hit_rate(self) -> float:
        return _mean(1.0 if r.hit else 0.0 for r in self.results)

    @property
    def recall(self) -> float:
        return _mean(len(r.found) / len(r.case.expected_sources) for r in self.results)

    @property
    def mrr(self) -> float:
        return _mean(1.0 / r.first_rank if r.first_rank else 0.0 for r in self.results)


def _mean(values: Any) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def load_cases(path: str | Path) -> list[EvalCase]:
    """Read cases from a JSON Lines file. Blank lines and lines starting with ``#`` are ignored."""
    cases: list[EvalCase] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            raw = json.loads(line)
            query = raw["query"]
            expected = raw["expected_sources"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ValueError(f"{path}:{number}: each line needs a JSON object with 'query' and 'expected_sources'") from exc
        if not isinstance(query, str) or not query.strip() or not isinstance(expected, list) or not expected:
            raise ValueError(f"{path}:{number}: 'query' must be text and 'expected_sources' a non-empty list")
        cases.append(EvalCase(str(raw.get("id") or f"case-{len(cases) + 1}"), query, tuple(str(s) for s in expected)))
    return cases


def _matches(doc: dict[str, Any], expected: str) -> bool:
    needle = expected.lower()
    return (
        needle in str(doc.get("title", "")).lower()
        or needle in str(doc.get("source_url", "")).lower()
        or needle in str(doc.get("blob_name", "")).lower()
    )


def evaluate(cases: list[EvalCase], search: Callable[[EvalCase], list[dict[str, Any]]], k: int = 5) -> EvalReport:
    """Run every case through *search* and score the top *k* results."""
    results = []
    for case in cases:
        docs = search(case)[:k]
        result = CaseResult(case, [f"{d.get('title', '')} | {d.get('source_url', '')}" for d in docs])
        for expected in case.expected_sources:
            rank = next((i for i, d in enumerate(docs, start=1) if _matches(d, expected)), None)
            if rank is not None:
                result.found.append(expected)
                result.first_rank = rank if result.first_rank is None else min(result.first_rank, rank)
        results.append(result)
    return EvalReport(k=k, results=results)


def format_report(reports: dict[str, EvalReport], *, show_misses: bool = True) -> str:
    """A comparison table (one row per mode) followed by the cases that found nothing."""
    lines = [f"{'mode':<10} {'cases':>5} {'hit rate':>9} {'recall@k':>9} {'MRR':>6}"]
    for mode, report in reports.items():
        lines.append(f"{mode:<10} {len(report.results):>5} {report.hit_rate:>9.2f} {report.recall:>9.2f} {report.mrr:>6.2f}")
    if show_misses:
        for mode, report in reports.items():
            for result in (r for r in report.results if not r.hit):
                lines.append(f"\n[{mode}] no expected source in the top {report.k} for {result.case.id}: {result.case.query}")
                lines.append(f"    expected: {', '.join(result.case.expected_sources)}")
                lines.extend(f"    got: {item}" for item in result.retrieved[:3])
    return "\n".join(lines)
