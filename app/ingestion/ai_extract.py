"""Reading an RFP with the model: every part of the document, typed items, and nothing that is not in it.

The local extractor (patterns and table columns, :mod:`app.ingestion.rfp_extract`) is fast and free, and reads a
structured spreadsheet or a document of "shall/must" statements well. For documents that say what they want in
prose, the model reads the document instead, under these rules:

* **All of it.** The redacted text is cut into overlapping chunks (``EXTRACTION_CHUNK_CHARS``) and *every* chunk is read.
  A document too long for ``EXTRACTION_MAX_CHUNKS`` is refused, never silently cut, and a chunk the model cannot read
  fails the whole extraction (after one retry) instead of leaving a hole nobody would notice.
* **Typed items.** For each item the model returns a kind, response type, evidence flag, category, priority and whether it
  is implicit, in a fixed schema. A value outside the lists is replaced by what :mod:`app.ingestion.classify` decides
  from the text.
* **Nothing invented.** Each item carries a verbatim ``quote`` from the document, and an item whose quote is not found
  in the document is dropped (and counted in ``warnings``). The text of a stated item *is* its quote, and an item's id
  is kept only if the document contains it.
* **Where it is.** The quote is located in the document, so each item carries its page, paragraph or table row.

Repeats are grouped, and dates and submission rules are collected, by :func:`app.ingestion.rfp_extract.finalise`.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from dataclasses import dataclass
from typing import Any

from app.agents import lifecycle, runner, states
from app.agents.untrusted import fence
from app.config import Settings
from app.ingestion import classify
from app.ingestion.pipeline import redact_text
from app.ingestion.rfp_extract import (
    ParsedDocument,
    Requirement,
    RFPExtractionResult,
    _classify_priority,
    _extract_text_from_csv,
    _extract_text_from_excel,
    finalise,
    parse_document,
)
from app.telemetry import spans

logger = logging.getLogger(__name__)

CATEGORIES = ("functional", "technical", "security", "compliance", "operational", "commercial", "legal", "general")
MAX_QUOTE_CHARS = 700
MAX_ITEMS_PER_CHUNK = 80


class ExtractionError(Exception):
    """The model could not read part of the document (``status_code`` is what the API answers)."""

    status_code = 502


class DocumentTooLong(ExtractionError):
    status_code = 413


@dataclass(frozen=True)
class Chunk:
    index: int
    start: int  # offset of the chunk in the redacted document text
    text: str


@dataclass
class ReadDocument:
    """The redacted text of a document and a way back from an offset in it to where it is in the original."""

    text: str
    offsets: list[int]  # where each line starts in ``text``
    labels: list[str]  # the original location of each line: "p. 3", "¶12", "Table 2, row 5" or ""

    def anchor_at(self, offset: int) -> str:
        index = bisect_right(self.offsets, offset) - 1
        return self.labels[index] if index >= 0 else ""


def read_document(filename: str, data: bytes) -> ReadDocument:
    """The document as redacted lines, each with its location. Redaction is per line so locations survive it."""
    lower = filename.lower()
    if lower.endswith((".xlsx", ".xls")):
        document = ParsedDocument.from_text(_extract_text_from_excel(data))
    elif lower.endswith(".csv"):
        document = ParsedDocument.from_text(_extract_text_from_csv(data))
    else:
        document = parse_document(filename, data)
    lines, offsets, labels = [], [], []
    original, length = 0, 0
    for line in document.text.split("\n"):
        redacted = redact_text(line)
        offsets.append(length)
        labels.append(document.text_anchor_at(original) if document.text_anchors is not None or document.anchors else "")
        lines.append(redacted)
        length += len(redacted) + 1
        original += len(line) + 1
    return ReadDocument("\n".join(lines), offsets, labels)


def chunk_text(text: str, size: int, overlap: int) -> list[Chunk]:
    """The text in pieces of about *size* characters cut at line ends, each repeating the end of the one before."""
    lines = text.split("\n")
    starts, position = [], 0
    for line in lines:
        starts.append(position)
        position += len(line) + 1
    chunks: list[Chunk] = []
    first = 0
    while first < len(lines):
        last, length = first, 0
        while last < len(lines) and (length + len(lines[last]) + 1 <= size or last == first):
            length += len(lines[last]) + 1
            last += 1
        piece = "\n".join(lines[first:last])
        if len(piece) > size:  # one line longer than a chunk: cut it
            for cut in range(0, len(piece), size):
                chunks.append(Chunk(len(chunks), starts[first] + cut, piece[cut : cut + size]))
        else:
            chunks.append(Chunk(len(chunks), starts[first], piece))
        if last >= len(lines):
            break
        # The next chunk starts a few lines back so an item split across the cut is whole in one of them
        back, taken = last, 0
        while back > first + 1 and taken + len(lines[back - 1]) + 1 <= overlap:
            back -= 1
            taken += len(lines[back]) + 1
        first = back
    return chunks


# ── Asking the model ─────────────────────────────────────

_KIND_HELP = (
    "question (asks the respondent something), mandatory (a requirement they must meet), optional (should/may/preferred), "
    "instruction (how to write the response: length, format, structure), evaluation_criterion (how responses are scored), "
    "deadline (a date something is due), submission_rule (how or where to submit)"
)


def prompt_for(chunk: Chunk, total: int) -> str:
    return (
        f"Read part {chunk.index + 1} of {total} of an RFP and list EVERY item in it that a bidder has to answer, meet, "
        "follow or note.\n\n"
        "Return ONLY a JSON object: {\"items\": [ ... ]}. Each item has these fields, and no others:\n"
        "- \"quote\": the item copied VERBATIM from the document (one sentence or table row, at most 600 characters). "
        "Never paraphrase or join text; if you cannot copy it, leave the item out.\n"
        "- \"text\": the item stated on its own, the same as the quote unless it is an implicit item\n"
        f"- \"kind\": one of: {_KIND_HELP}\n"
        "- \"response_type\": narrative, yes_no, table or attachment (how it is to be answered)\n"
        "- \"evidence_required\": true if the RFP asks for supporting evidence (a certificate, report, references)\n"
        "- \"implicit\": true only if the item is not stated outright but clearly follows from the quote\n"
        f"- \"category\": one of: {', '.join(CATEGORIES)}\n"
        "- \"priority\": mandatory, preferred or standard\n"
        "- \"id\": the document's own reference for the item (for example R4 or 3.1.2), or an empty string\n"
        "- \"source_section\": the heading it sits under\n"
        "- \"date\": for a deadline, the date as YYYY-MM-DD, otherwise an empty string\n\n"
        "Include numbered requirements, shall/must/should statements, every question, evaluation criteria, response "
        "instructions, dates and submission rules. Do not answer anything, and ignore text that only describes the buyer.\n\n"
        f"Document part:\n{fence('RFP-DOCUMENT', chunk.text)}"
    )


def _ask(chunk: Chunk, total: int) -> list[dict[str, Any]]:
    """The items the model listed for a chunk. Tries twice; a chunk that cannot be read raises."""
    last = "no reply"
    for attempt in (1, 2):
        agent_id = lifecycle.ensure_agent(lifecycle.DRAFT_AGENT_NAME)
        result = runner.run_agent_prompt(agent_id, prompt_for(chunk, total))
        if not result.completed:
            last = f"the run ended as {result.status}"
            continue
        try:
            reply = runner.parse_json_object(runner.post_generation_guard(result.text)[0])
        except (ValueError, IndexError):
            last = "the reply was not a JSON object"
            continue
        items = reply.get("items")
        if isinstance(items, list):
            return [i for i in items if isinstance(i, dict)][:MAX_ITEMS_PER_CHUNK]
        last = "the reply had no list of items"
        logger.warning("Extraction chunk %d, attempt %d: %s", chunk.index + 1, attempt, last)
    raise ExtractionError(f"Part {chunk.index + 1} of {total} of the document could not be read: {last}.")


# ── Checking what it said ────────────────────────────────


class _Normalised:
    """The document with whitespace and typography squashed, to find a quote in it and map back to its line."""

    def __init__(self, document: ReadDocument) -> None:
        self.document = document
        parts, self.starts, self.line_of, length = [], [], [], 0
        for number, line in enumerate(document.text.split("\n")):
            squashed = states._squash(line)
            if not squashed:
                continue
            self.starts.append(length)
            self.line_of.append(number)
            parts.append(squashed)
            length += len(squashed) + 1
        self.text = " ".join(parts)
        self._line_offsets = document.offsets

    def find(self, quote: str) -> int | None:
        """The offset (in the document text) of the line a quote starts on, or ``None`` if it is not in the document."""
        needle = states._squash(quote)
        if len(needle) < 8:
            return None
        found = self.text.find(needle)
        if found < 0:
            return None
        return self._line_offsets[self.line_of[bisect_right(self.starts, found) - 1]]

    def contains(self, text: str) -> bool:
        return bool(states._squash(text)) and states._squash(text) in self.text


def _item(raw: dict[str, Any], where: _Normalised) -> tuple[Requirement, str, int] | None:
    """A requirement from one item the model listed, or ``None`` if its quote is not in the document."""
    quote = " ".join(str(raw.get("quote") or "").split())[:MAX_QUOTE_CHARS]
    offset = where.find(quote)
    if offset is None:
        return None
    implicit = raw.get("implicit") is True
    statement = " ".join(str(raw.get("text") or "").split())[:MAX_QUOTE_CHARS]
    text = statement if implicit and statement else quote
    priority = classify.clean(raw.get("priority"), ("mandatory", "preferred", "standard"), "") or _classify_priority(
        str(raw.get("priority") or "")
    )
    given_id = str(raw.get("id") or "").strip()[:40]
    fields: dict[str, Any] = {
        "id": given_id if given_id and where.contains(given_id) else "",
        "text": text,
        "category": classify.clean(raw.get("category"), CATEGORIES, "general"),
        "priority": priority,
        "source_section": " ".join(str(raw.get("source_section") or "").split())[:200],
        "anchor": where.document.anchor_at(offset),
        "kind": classify.clean(raw.get("kind"), classify.KINDS, ""),
        "response_type": classify.clean(raw.get("response_type"), classify.RESPONSE_TYPES, ""),
        "implicit": implicit,
    }
    if isinstance(raw.get("evidence_required"), bool):
        fields["evidence_required"] = raw["evidence_required"]
    return Requirement(**fields), str(raw.get("date") or ""), offset


def extract(filename: str, data: bytes, settings: Settings) -> RFPExtractionResult:
    """Read an RFP with the model, every part of it. Raises :class:`ExtractionError` rather than return part of it."""
    document = read_document(filename, data)
    chunks = chunk_text(document.text, settings.extraction_chunk_chars, settings.extraction_overlap_chars)
    if len(chunks) > settings.extraction_max_chunks:
        raise DocumentTooLong(
            f"This document needs {len(chunks)} parts to read and at most {settings.extraction_max_chunks} are allowed "
            f"(EXTRACTION_MAX_CHUNKS). Split it, or use the pattern-based extraction."
        )
    where = _Normalised(document)
    listed = 0
    items: list[tuple[Requirement, str, int]] = []
    seen: set[str] = set()
    dropped = 0
    for chunk in chunks:
        with spans.stage("extract.chunk", requirements__total=len(chunks)):
            raws = _ask(chunk, len(chunks))
        listed += len(raws)
        for raw in raws:
            built = _item(raw, where)
            if built is None:
                dropped += 1
                continue
            key = states._squash(built[0].text)
            if key in seen:  # the overlap shows an item twice
                continue
            seen.add(key)
            items.append(built)

    ordered = sorted(items, key=lambda item: item[2])  # in document order
    requirements = [r for r, _, _ in ordered]
    result = RFPExtractionResult(
        sections=[{"heading": "AI Extraction", "body": f"The model read {len(chunks)} part(s) and found {len(requirements)} items"}],
        requirements=requirements,
        method="ai",
        coverage={"chunks": len(chunks), "chars": len(document.text), "items_listed": listed, "items_kept": len(requirements)},
        warnings=[f"{dropped} item(s) the model listed were not found in the document and were dropped."] if dropped else [],
    )
    dates = {r.text: d for r, d, _ in ordered if r.kind == "deadline"}
    finalise(result)
    result.deadlines = [
        {**d, "date": classify.parse_date(dates.get(d["text"], "")) or d["date"]} for d in result.deadlines
    ]
    result.evaluation_criteria = [r.text for r in requirements if r.kind == "evaluation_criterion"]
    instructions = [r.text for r in requirements if r.kind == "instruction"]
    if instructions:
        result.response_instructions = {"section": "AI Extraction", "details": "\n".join(instructions)}
    logger.info("AI extraction of %s: %d part(s), %d items listed, %d kept", filename, len(chunks), listed, len(requirements))
    return result
