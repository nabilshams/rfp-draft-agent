"""The golden set: RFP specs, a fixture knowledge base, and an in-memory search index over it."""

from __future__ import annotations

import json
import math
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.evalkit.metrics import words

GOLDEN_DIR = Path(__file__).resolve().parents[2] / "evals" / "golden"

_STOP = frozenset("a an and are as at be by for from has have in is it its of on or the to will with your our".split())


@dataclass(frozen=True)
class RfpCase:
    id: str
    filename: str
    spec: dict[str, Any]

    @property
    def requirements(self) -> list[dict[str, Any]]:
        return self.spec["requirements"]


def load_cases(golden_dir: Path = GOLDEN_DIR) -> list[RfpCase]:
    cases = []
    for path in sorted((golden_dir / "rfps").glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))
        cases.append(RfpCase(id=spec["id"], filename=spec["filename"], spec=spec))
    if not cases:
        raise FileNotFoundError(f"no RFP specs in {golden_dir / 'rfps'}")
    return cases


def load_kb(golden_dir: Path = GOLDEN_DIR) -> list[dict[str, Any]]:
    return json.loads((golden_dir / "kb.json").read_text(encoding="utf-8"))["documents"]


class GoldenIndex:
    """Stands in for the Azure AI Search client: keyword search over the fixture knowledge base.

    It returns hits shaped like the real service's (content, title, source_url, provenance, ``@search.score``), so
    everything after the index (redaction, provenance ranking, reference links) is the real code. Access filters
    are not applied: the golden knowledge base belongs to one fictional tenant.
    """

    def __init__(self, documents: list[dict[str, Any]], blob_url: str) -> None:
        self._chunks: list[dict[str, Any]] = []
        for doc in documents:
            for chunk in doc["chunks"]:
                tokens = [w for w in words(chunk["text"] + " " + chunk.get("section", "")) if w not in _STOP]
                self._chunks.append(
                    {
                        "content": chunk["text"],
                        "title": doc["title"],
                        "source_url": f"{blob_url}/{doc['title']}",
                        "source_type": doc.get("source_type", ""),
                        "authority": doc.get("authority", ""),
                        "effective_date": f"{doc['effective_date']}T00:00:00Z" if doc.get("effective_date") else None,
                        "version": doc.get("version", ""),
                        "approval_status": doc.get("approval_status", ""),
                        "section": chunk.get("section", ""),
                        "page": chunk.get("page"),
                        "_tokens": Counter(tokens),
                        "_length": max(len(tokens), 1),
                    }
                )
        frequency: Counter[str] = Counter()
        for chunk in self._chunks:
            frequency.update(chunk["_tokens"].keys())
        total = len(self._chunks)
        self._idf = {t: math.log(1 + total / n) for t, n in frequency.items()}

    def search(self, **options: Any) -> list[dict[str, Any]]:
        query = {w for w in words(options.get("search_text", "")) if w not in _STOP}
        scored = []
        for chunk in self._chunks:
            hits = [t for t in query if t in chunk["_tokens"]]
            if not hits:
                continue
            score = sum(self._idf[t] for t in hits) / math.sqrt(chunk["_length"])
            result = {k: v for k, v in chunk.items() if not k.startswith("_")}
            result["@search.score"] = round(score, 4)
            scored.append(result)
        scored.sort(key=lambda r: r["@search.score"], reverse=True)
        return scored[: options.get("top") or 5]
