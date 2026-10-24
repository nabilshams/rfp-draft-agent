"""Read-only checks of a running deployment, for operators: ``python -m app.ops.check <what> [id]``.

Run inside the container (``az containerapp exec ... --command "python -m app.ops.check project <id>"``): the data is
behind private endpoints, so this is the way to look at it. It prints counts and metadata, **never the text of an
answer or a source, and only a short label of a requirement**, and changes nothing.

* ``project <id>``  the project's record, its items by kind, status and state, and its audit trail
* ``index``         how many chunks the knowledge-base index holds, and how many carry provenance (was it re-indexed?)
* ``outputs [prefix]``  the generated documents (names, sizes, times); for a ``.docx``, the figures it states that
                    appear nowhere in the knowledge base or the project's requirements (candidates for invention)
"""

from __future__ import annotations

import io
import sys
from collections import Counter

from app import azure_clients
from app.agents.grounding import figures_in
from app.config import Settings, get_settings
from app.ingestion.provenance import APPROVAL_STATUSES, SOURCE_TYPES
from app.ledger import store as ledger_store
from app.ledger.store import LedgerStore

LABEL = 70  # characters of a requirement shown
MAX_CHUNKS = 5000


def _count(reqs: list[dict], key: str) -> dict[str, int]:
    return dict(Counter(str(r.get(key) or "-") for r in reqs))


def project_report(store: LedgerStore, project_id: str) -> list[str]:
    project = store.get(project_id, "project")
    if project is None:
        return [f"No project {project_id}"]
    keep = ("name", "rfp_id", "requirement_count", "deadlines", "submission_rules", "extraction", "owner_name")
    lines = ["PROJECT " + ", ".join(f"{k}={project.get(k)!r}" for k in keep)]
    reqs = store.query(project_id, "requirement", order_by="order")
    lines.append(
        f"ITEMS {len(reqs)} kind={_count(reqs, 'item_kind')} response={_count(reqs, 'response_type')} "
        f"status={_count(reqs, 'status')} state={_count(reqs, 'state')} confidence={_count(reqs, 'confidence')} "
        f"verification={_count(reqs, 'verification')}"
    )
    lines.append(
        f"      evidence_required={sum(1 for r in reqs if r.get('evidence_required'))} "
        f"implicit={sum(1 for r in reqs if r.get('implicit'))} repeats={sum(1 for r in reqs if r.get('duplicate_of'))} "
        f"answered={sum(1 for r in reqs if r.get('answer_version'))}"
    )
    for r in reqs:
        lines.append(
            f"  {r['rid']:<8} {str(r.get('item_kind') or '-'):<20} {str(r.get('response_type') or '-'):<10} "
            f"{'ev' if r.get('evidence_required') else '--'} {'imp' if r.get('implicit') else '---'} "
            f"{str(r.get('duplicate_of') or ''):<6} {str(r.get('priority') or ''):<10} {str(r.get('anchor') or ''):<12} "
            f"| {' '.join(str(r['text']).split())[:LABEL]}"
        )
    events = store.query(project_id, "event", order_by="at")
    lines.append("EVENTS " + ", ".join(f"{e['action']}{'/' + e['rid'] if e.get('rid') else ''}" for e in events))
    return lines


def index_report(settings: Settings) -> list[str]:
    client = azure_clients.search_client(settings)

    def count(filter_: str | None = None) -> int:
        return client.search(search_text="*", filter=filter_, include_total_count=True, top=0).get_count()

    total = count()
    by_type = {t: count(f"source_type eq '{t}'") for t in SOURCE_TYPES}
    by_status = {s: count(f"approval_status eq '{s}'") for s in APPROVAL_STATUSES}
    return [
        f"INDEX {settings.ai_search_index_name}: {total} chunks",
        f"  source_type={by_type}  untyped={total - sum(by_type.values())}",
        f"  approval_status={by_status}  none={total - sum(by_status.values())}",
        f"  chunks with a page number={count('page ge 1')}",
    ]


def _knowledge_text(settings: Settings) -> str:
    client = azure_clients.search_client(settings)
    return "\n".join(
        str(hit.get("content") or "")
        for hit in client.search(search_text="*", select=["content"], top=MAX_CHUNKS)
    )


def _docx_text(data: bytes) -> str:
    import docx

    document = docx.Document(io.BytesIO(data))
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        parts += [cell.text for row in table.rows for cell in row.cells]
    return "\n".join(parts)


def figures_not_in(document_text: str, source_text: str) -> list[str]:
    """Figures (percentages, amounts, counts) the document states that the sources do not contain."""
    return sorted(figures_in(document_text) - figures_in(source_text))


def outputs_report(settings: Settings, prefix: str = "") -> list[str]:
    container = azure_clients.container(settings, settings.storage_output_container)
    blobs = list(container.list_blobs(name_starts_with=prefix or None))
    lines = ["OUTPUTS"] + [f"  {b.name}  {b.size} bytes  {b.last_modified:%Y-%m-%d %H:%M}" for b in blobs]
    docs = [b for b in blobs if b.name.endswith(".docx")]
    if docs:
        sources = _knowledge_text(settings)
        for blob in docs:
            text = _docx_text(container.download_blob(blob.name).readall())
            extra = figures_not_in(text, sources)
            lines.append(f"  {blob.name}: {len(text)} chars, figures not in the knowledge base: {extra or 'none'}")
    return lines


def main(argv: list[str]) -> int:
    what = argv[0] if argv else ""
    arg = argv[1] if len(argv) > 1 else ""
    settings = get_settings()
    if what == "project" and arg:
        store = ledger_store.store_for(settings)
        if store is None:
            print("The ledger is not configured")
            return 1
        out = project_report(store, arg)
    elif what == "index":
        out = index_report(settings)
    elif what == "outputs":
        out = outputs_report(settings, arg)
    else:
        print(__doc__)
        return 2
    print("\n".join(out))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
