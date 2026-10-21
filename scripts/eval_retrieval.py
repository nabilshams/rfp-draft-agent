"""Compare keyword, hybrid and semantic retrieval on your own knowledge base.

    python scripts/eval_retrieval.py evals/retrieval_cases.example.jsonl \
        --client-id demo-client --project-id demo-project --oid <your object id> --k 5

Each line of the cases file is a JSON object: {"id": "...", "query": "...", "expected_sources": ["..."]}.
An expected source is a substring of a document's title or URL. Searches run with the access filters of
the given user (--oid / --groups), so use someone who may read the documents the cases refer to.
Uses the same settings (.env / environment) and Azure sign-in as the app.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents import kb_search  # noqa: E402
from app.evaluation import evaluate, format_report, load_cases  # noqa: E402

MODES = ("keyword", "hybrid", "semantic")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("cases", help="JSON Lines file of evaluation cases")
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--solution-area")
    parser.add_argument("--oid", required=True, help="object ID of the user the searches run as")
    parser.add_argument("--groups", default="", help="comma-separated group IDs of that user")
    parser.add_argument("--k", type=int, default=5, help="results considered per query (default 5)")
    parser.add_argument("--modes", default=",".join(MODES), help=f"comma-separated subset of {', '.join(MODES)}")
    args = parser.parse_args(argv)

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    unknown = [m for m in modes if m not in MODES]
    if unknown:
        parser.error(f"unknown mode(s): {', '.join(unknown)}")

    cases = load_cases(args.cases)
    vectors = kb_search.embed_queries([c.query for c in cases]) if any(m != "keyword" for m in modes) else []
    vector_by_id = {c.id: v for c, v in zip(cases, vectors)}
    if vectors and not any(vectors):
        print("warning: embeddings are unavailable, so hybrid/semantic results are keyword-only", file=sys.stderr)

    def searcher(mode: str):
        def search(case):
            return kb_search.search_kb(
                query=case.query,
                vector=None if mode == "keyword" else vector_by_id.get(case.id),
                client_id=args.client_id,
                project_id=args.project_id,
                solution_area=args.solution_area,
                user_oid=args.oid,
                user_groups=[g for g in args.groups.split(",") if g],
                top_k=args.k,
                min_score=0.0,
                semantic=(mode == "semantic"),
            )

        return search

    reports = {mode: evaluate(cases, searcher(mode), k=args.k) for mode in modes}
    print(format_report(reports))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
