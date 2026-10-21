"""Console logging, and a log stream that is broadcast to browsers over WebSocket."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import UTC, datetime

from fastapi import WebSocket

CONSOLE_HANDLER_NAME = "rfp-console"

ws_clients: set[WebSocket] = set()
log_buffer: deque[dict] = deque(maxlen=200)  # keep last 200 log entries
_main_loop: asyncio.AbstractEventLoop | None = None  # set at startup; log records may come from any thread
_ws_send_tasks: set[asyncio.Task] = set()  # keep references so in-flight sends aren't garbage collected


def set_main_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    global _main_loop
    _main_loop = loop


async def _send_log(ws: WebSocket, entry: dict) -> None:
    try:
        await ws.send_json(entry)
    except Exception:
        ws_clients.discard(ws)


def _broadcast(entry: dict) -> None:
    """Runs on the event loop: start a send to every connected client."""
    for ws in list(ws_clients):
        task = asyncio.get_running_loop().create_task(_send_log(ws, entry))
        _ws_send_tasks.add(task)
        task.add_done_callback(_ws_send_tasks.discard)


class WebSocketLogHandler(logging.Handler):
    """Logging handler that broadcasts log records to connected WebSocket clients."""

    def emit(self, record: logging.LogRecord) -> None:
        entry = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": self.format(record),
        }
        log_buffer.append(entry)

        loop = _main_loop
        if loop is None or loop.is_closed() or not ws_clients:
            return
        try:
            loop.call_soon_threadsafe(_broadcast, entry)
        except RuntimeError:
            pass  # loop is shutting down


def configure_logging(level_name: str) -> None:
    """Send application logs to the console (container log stream) at the configured level.

    The root logger has no console handler otherwise, so records from ``app.*`` loggers would
    never leave the process except through the WebSocket handler.
    """
    root = logging.getLogger()
    if not any(h.get_name() == CONSOLE_HANDLER_NAME for h in root.handlers):
        handler = logging.StreamHandler()
        handler.set_name(CONSOLE_HANDLER_NAME)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s"))
        root.addHandler(handler)
    root.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    # These log every outgoing HTTP request at INFO.
    for noisy in ("azure", "httpx", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def install_ws_log_handler() -> None:
    """Install the WebSocket handler on the root logger."""
    handler = WebSocketLogHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(name)s - %(message)s"))
    root = logging.getLogger()
    # Avoid duplicates on reload
    if not any(isinstance(h, WebSocketLogHandler) for h in root.handlers):
        root.addHandler(handler)
