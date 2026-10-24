"""Stage spans, token counts and cost estimates, without ever recording content.

A draft is one trace: a ``draft`` span (job id, approach, totals) with a child ``requirement`` span per requirement,
and under each the stages that ran: ``kb.search``, ``llm.run`` (model, tokens, latency, outcome, cost) and ``ground``
(figures removed). Sections add ``section.classify`` and ``section.narrative``; storing the documents is ``outputs``.
They reach Application Insights as dependencies (see ``infra/queries/``).

**No content in telemetry.** A span accepts only the attributes named in :data:`ALLOWED`, only numbers, booleans and
short identifiers, and an exception is recorded by type, never by message. Nothing a user wrote, that a document
contains or that a model produced can be attached by accident, because there is no way to pass free text.

The totals for a draft are kept by a :class:`Meter` that is carried in a context variable, so code deep in the pipeline
(the model call) can add to it without every function passing it along.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

logger = logging.getLogger(__name__)

TRACER_NAME = "rfp-draft-agent"

# Every attribute a span may carry. Adding a name here is a decision that it can never hold content.
ALLOWED = frozenset(
    {
        # identifiers and enumerations
        "job.id", "rfp.id", "approach", "requirement.id", "section.name_hash", "outcome", "error.type", "web_search",
        "llm.model", "llm.status", "kb.mode", "answer.state", "verifier.model", "verification.status",
        # counts and measurements
        "requirements.total", "requirements.done", "sections", "llm.calls", "llm.input_tokens", "llm.output_tokens",
        "llm.total_tokens", "llm.cost_usd", "llm.latency_ms", "kb.results", "kb.top_k", "figures.removed",
        "citations", "evidence", "claims", "claims.flagged", "claims.by_rule", "context.docs", "prompt.chars", "answer.chars", "outputs.bytes",
    }
)
_IDENTIFIER = re.compile(r"^[\w.:/+\- ]{0,64}$")
# Names only: a value that looks like a sentence is not an identifier
_MAX_WORDS = 4


def _acceptable(value: Any) -> bool:
    if isinstance(value, bool | int | float):
        return True
    if isinstance(value, str):
        return bool(_IDENTIFIER.match(value)) and len(value.split()) <= _MAX_WORDS
    return False


def safe_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """The attributes that may be recorded: allowed names with number, boolean or short identifier values."""
    kept = {}
    for key, value in attributes.items():
        if key in ALLOWED and value is not None and _acceptable(value):
            kept[key] = value
        else:
            logger.debug("Telemetry attribute %s dropped (not allowed or not a safe value)", key)
    return kept


def get_tracer(name: str = TRACER_NAME) -> trace.Tracer:
    return trace.get_tracer(name)


def annotate(**attributes: Any) -> None:
    """Add safe attributes to the span that is current (for example the requirement being answered)."""
    Stage(trace.get_current_span()).set(**attributes)


# ── Cost ─────────────────────────────────────────────────


def cost_usd(input_tokens: int, output_tokens: int, input_price: float, output_price: float) -> float:
    """Estimated dollars for a number of tokens, from prices per million tokens."""
    return round((input_tokens * input_price + output_tokens * output_price) / 1_000_000, 6)


@dataclass
class Meter:
    """Token and cost totals for one draft (or one request). Safe to add to from several threads."""

    input_price: float = 0.0
    output_price: float = 0.0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    figures_removed: int = 0
    other_cost: float = 0.0  # calls priced unlike the default (see add_usage), and their tokens
    other_input_tokens: int = 0
    other_output_tokens: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_usage(self, input_tokens: int, output_tokens: int, prices: tuple[float, float] | None = None) -> float:
        """Count one model call; returns its estimated cost. *prices* are for a model priced unlike the default."""
        cost = cost_usd(input_tokens, output_tokens, *(prices or (self.input_price, self.output_price)))
        with self._lock:
            self.calls += 1
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
            if prices:
                self.other_cost += cost
                self.other_input_tokens += input_tokens
                self.other_output_tokens += output_tokens
        return cost

    def add_figures_removed(self, count: int) -> None:
        with self._lock:
            self.figures_removed += count

    @property
    def cost(self) -> float:
        default = cost_usd(
            self.input_tokens - self.other_input_tokens,
            self.output_tokens - self.other_output_tokens,
            self.input_price,
            self.output_price,
        )
        return round(default + self.other_cost, 6)

    def totals(self) -> dict[str, Any]:
        return {
            "llm.calls": self.calls,
            "llm.input_tokens": self.input_tokens,
            "llm.output_tokens": self.output_tokens,
            "llm.total_tokens": self.input_tokens + self.output_tokens,
            "llm.cost_usd": self.cost,
            "figures.removed": self.figures_removed,
        }


_meter: contextvars.ContextVar[Meter | None] = contextvars.ContextVar("telemetry_meter", default=None)


def current_meter() -> Meter | None:
    return _meter.get()


def meter_from_settings(settings: Any) -> Meter:
    return Meter(
        input_price=settings.model_input_price_per_million, output_price=settings.model_output_price_per_million
    )


# ── Spans ────────────────────────────────────────────────


class Stage:
    """A running span that takes only safe attributes. Write a dotted name with a double underscore:
    ``stage.set(requirement__id="R1")`` records ``requirement.id``."""

    def __init__(self, span: trace.Span) -> None:
        self._span = span

    def set(self, **attributes: Any) -> None:
        for key, value in safe_attributes({k.replace("__", "."): v for k, v in attributes.items()}).items():
            self._span.set_attribute(key, value)

    def set_outcome(self, outcome: str) -> None:
        self.set(outcome=outcome)


@contextlib.contextmanager
def stage(name: str, **attributes: Any) -> Iterator[Stage]:
    """Run a block inside a span called *name*, timing it and recording how it ended.

    Use around a block that does not ``yield``: a span's context is tied to the thread that opened it.
    An exception ends the span with ``outcome=error`` and the exception's type, then propagates.
    """
    with get_tracer().start_as_current_span(name, record_exception=False, set_status_on_exception=False) as span:
        handle = Stage(span)
        handle.set(**attributes)
        try:
            yield handle
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR))
            handle.set(outcome="error", error__type=type(exc).__name__)
            raise


@contextlib.contextmanager
def meter_scope(meter: Meter | None) -> Iterator[None]:
    token = _meter.set(meter)
    try:
        yield
    finally:
        _meter.reset(token)


class DraftTrace:
    """The root span of one draft and its meter, kept open across the draft's generator.

    A generator can be resumed on a different thread at each step, so the span is never made "current" for the whole
    draft. Instead :meth:`step` makes it current for one step at a time (see ``iterate``).
    """

    def __init__(self, name: str, meter: Meter, **attributes: Any) -> None:
        self.meter = meter
        self.span = get_tracer().start_span(name)
        self.stage = Stage(self.span)
        self.stage.set(**attributes)
        self._context = trace.set_span_in_context(self.span)
        self._ended = False

    @contextlib.contextmanager
    def step(self) -> Iterator[None]:
        """Make this draft's span and meter current for one unit of work, on the calling thread."""
        token = otel_context.attach(self._context)
        try:
            with meter_scope(self.meter):
                yield
        finally:
            otel_context.detach(token)

    def iterate(self, events: Iterator[Any]) -> Iterator[Any]:
        """Yield *events*, running each step of the underlying generator inside this draft's trace."""
        while True:
            with self.step():
                try:
                    event = next(events)
                except StopIteration:
                    return
            yield event

    def finish(self, outcome: str, **attributes: Any) -> None:
        if self._ended:
            return
        self._ended = True
        self.stage.set(outcome=outcome, **self.meter.totals(), **attributes)
        if outcome in ("error", "failed"):
            self.span.set_status(Status(StatusCode.ERROR))
        self.span.end()
        logger.info(
            "Draft finished: outcome=%s calls=%d tokens=%d cost_usd=%.4f",
            outcome, self.meter.calls, self.meter.input_tokens + self.meter.output_tokens, self.meter.cost,
        )


@contextlib.contextmanager
def draft_scope(name: str, settings: Any, **attributes: Any) -> Iterator[DraftTrace]:
    """A draft that runs in one block on one thread (not a generator): its span, meter and outcome."""
    draft = DraftTrace(name, meter_from_settings(settings), **attributes)
    outcome = "ok"
    try:
        with draft.step():
            yield draft
    except Exception as exc:
        outcome = "error"
        draft.stage.set(error__type=type(exc).__name__)
        raise
    finally:
        draft.finish(outcome)
