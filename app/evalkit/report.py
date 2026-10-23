"""JSON and Markdown reports for an evaluation run, and the committed baseline."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from app.evalkit import metrics
from app.evalkit.run import EvalRun

_LABELS = {
    "extraction_recall": ("Extraction recall", "share of the true requirements that were found"),
    "extraction_precision": ("Extraction precision", "share of extracted items that are true requirements"),
    "retrieval_hit_rate": ("Retrieval hit rate", "the expected source was among the retrieved documents"),
    "retrieval_mrr": ("Retrieval MRR", "how high the expected source ranked (1.0 = first)"),
    "answer_rate": ("Answer rate", "requirements for which the model produced a usable answer"),
    "citation_support_rate": ("Citation support", "citations whose quote is really in the cited document"),
    "unsupported_claim_rate_raw": ("Unsupported figures (model)", "answers where the model wrote a figure not in its sources"),
    "unsupported_claim_rate_final": ("Unsupported figures (delivered)", "the same, after the app removed them (should be 0)"),
    "abstention_correctness": ("Abstention correctness", "declined when the knowledge base has nothing, answered when it has"),
    "state_correctness": ("State correctness", "answers that ended in the right state (answered, or a placeholder for a person)"),
    "false_answer_rate": ("False answers", "unsupported requirements that were answered anyway"),
    "answer_correctness": ("Answer correctness", "answers that state the expected fact and not the stale one"),
    "compliance_correctness": ("Compliance correctness", "Yes/Partial/No matches what the sources support"),
}


def to_dict(run: EvalRun, *, input_price: float | None = None, output_price: float | None = None) -> dict[str, Any]:
    summary = dict(run.metrics)
    cost = metrics.estimate_cost(summary, input_price, output_price)
    if cost is not None:
        summary["cost_per_requirement_usd"] = cost
    return {
        "variant": run.variant,
        "mode": run.mode,
        "metrics": summary,
        "cases": [asdict(c) for c in run.cases],
        "requirements": [asdict(s) for s in run.scores],
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1%}" if isinstance(value, float) and 0 <= value <= 1 else str(value)


def to_markdown(report: dict[str, Any], baseline: dict[str, Any] | None = None) -> str:
    summary = report["metrics"]
    lines = [
        f"# Evaluation report: {report['variant']} ({report['mode']})",
        "",
        f"{summary['requirements_expected']} golden requirements; {summary['requirements_extracted']} extracted.",
        "",
        "| Metric | Value | Baseline | Meaning |",
        "|---|---|---|---|",
    ]
    for name, (label, meaning) in _LABELS.items():
        before = _fmt(baseline.get(name)) if baseline else ""
        lines.append(f"| {label} | {_fmt(summary.get(name))} | {before} | {meaning} |")
    for name, label, unit in (
        ("latency_mean_s", "Latency per requirement (mean)", "s"),
        ("latency_p95_s", "Latency per requirement (p95)", "s"),
        ("input_tokens_per_requirement", "Input tokens per requirement", ""),
        ("output_tokens_per_requirement", "Output tokens per requirement", ""),
        ("cost_per_requirement_usd", "Estimated cost per requirement", "USD"),
    ):
        if summary.get(name) is not None:
            lines.append(f"| {label} | {summary[name]} {unit}".rstrip() + " | | recorded from the live run |")

    lines += ["", "## Cases", "", "| Case | File | Expected | Extracted | Matched | Missed | Extra |", "|---|---|---|---|---|---|---|"]
    for case in report["cases"]:
        lines.append(
            f"| {case['id']} | {case['filename']} | {case['expected']} | {case['extracted']} | {case['matched']} | "
            f"{', '.join(case['missed']) or '-'} | {len(case['extra'])} |"
        )
    extras = [(c["id"], text) for c in report["cases"] for text in c["extra"]]
    if extras:
        lines += ["", "Extracted but not in the golden list:", ""] + [f"- {case}: {text}" for case, text in extras]

    problems = [r for r in report["requirements"] if r["problems"]]
    lines += ["", f"## Problems ({len(problems)} of {len(report['requirements'])} requirements)", ""]
    if not problems:
        lines.append("None.")
    for r in problems:
        lines.append(f"- **{r['case']} / {r['id']}** {r['text'][:90]}: " + "; ".join(r["problems"]))
    return "\n".join(lines) + "\n"


def write_report(
    run: EvalRun,
    directory: Path,
    baseline: dict[str, Any] | None = None,
    **prices: float | None,
) -> dict[str, Any]:
    report = to_dict(run, **prices)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{run.variant}.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (directory / f"{run.variant}.md").write_text(to_markdown(report, baseline), encoding="utf-8")
    return report


def load_baseline(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def write_baseline(report: dict[str, Any], path: Path, tolerance: float) -> None:
    """The committed baseline: the headline metrics and the tolerance a later run is held to."""
    keep = {k: v for k, v in report["metrics"].items() if k in metrics.HIGHER_IS_BETTER + metrics.LOWER_IS_BETTER}
    payload = {"variant": report["variant"], "tolerance": tolerance, "metrics": keep}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
