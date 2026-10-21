"""Tests that blocking work stays off the event loop and that log streaming works from worker threads."""

import asyncio
import logging
import threading
import time
from collections import deque

import httpx

from app import main


async def test_health_stays_responsive_while_a_blocking_handler_runs(monkeypatch):
    def slow_answer(**_kwargs):
        time.sleep(1.5)
        return {"answer_markdown": "done"}

    monkeypatch.setattr("app.agents.foundry_agent.run_single_answer", slow_answer)
    body = {"question": "What is Azure?", "client_id": "c", "project_id": "p"}

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        slow_request = asyncio.create_task(client.post("/answer", json=body))
        await asyncio.sleep(0.2)  # let the slow request start

        started = time.perf_counter()
        health = await client.get("/health")
        elapsed = time.perf_counter() - started

        assert health.status_code == 200
        assert elapsed < 0.5, f"/health took {elapsed:.2f}s while a slow request was running"
        assert not slow_request.done()

        slow_response = await slow_request
        assert slow_response.status_code == 200
        assert slow_response.json() == {"answer_markdown": "done"}


class FakeWebSocket:
    def __init__(self, fail=False):
        self.fail = fail
        self.received: list[dict] = []

    async def send_json(self, data):
        if self.fail:
            raise RuntimeError("connection closed")
        self.received.append(data)


async def test_log_records_from_worker_threads_reach_websocket_clients(monkeypatch):
    healthy, broken = FakeWebSocket(), FakeWebSocket(fail=True)
    monkeypatch.setattr(main, "_main_loop", asyncio.get_running_loop())
    monkeypatch.setattr(main, "_ws_clients", {healthy, broken})
    monkeypatch.setattr(main, "_log_buffer", deque(maxlen=200))

    record = logging.LogRecord("test", logging.INFO, __file__, 1, "from a worker thread", None, None)
    worker = threading.Thread(target=main.WebSocketLogHandler().emit, args=(record,))
    worker.start()
    worker.join()
    await asyncio.sleep(0.1)  # let the loop run the scheduled broadcast

    assert [entry["message"] for entry in healthy.received] == ["from a worker thread"]
    assert broken not in main._ws_clients  # clients whose send fails are dropped
    assert healthy in main._ws_clients
    assert len(main._log_buffer) == 1
