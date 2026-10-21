"""Process-wide request counters for ``GET /metrics``."""

from __future__ import annotations

import threading

counters: dict[str, int] = {
    "rfp_uploads": 0,
    "extractions": 0,
    "drafts": 0,
    "answers": 0,
    "redaction_blocks": 0,
}
# Handlers run in worker threads, so increments must be serialised.
_lock = threading.Lock()


def bump(name: str) -> None:
    with _lock:
        counters[name] += 1
