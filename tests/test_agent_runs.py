"""Tests for run_agent_prompt: bounded polling, reply extraction and thread cleanup."""

import itertools
from types import SimpleNamespace

import pytest
from azure.ai.agents.models import MessageRole

from app.agents import foundry_agent


class FakeAgentsClient:
    """Minimal stand-in for AgentsClient that records thread deletions and run cancellations."""

    def __init__(self, statuses, reply="Hello", fail_delete=False, fail_message_create=False):
        self._statuses = list(statuses)
        self._reply = reply
        self._fail_delete = fail_delete
        self._fail_message_create = fail_message_create
        self.deleted: list[str] = []
        self.cancelled: list[str] = []
        self.messages_listed = False
        self.threads = SimpleNamespace(create=self._thread_create, delete=self._thread_delete)
        self.messages = SimpleNamespace(create=self._message_create, list=self._message_list)
        self.runs = SimpleNamespace(create=self._run_create, get=self._run_get, cancel=self._run_cancel)

    def _run(self, status):
        return SimpleNamespace(id="run-1", status=status, last_error="boom" if status == "failed" else None)

    def _thread_create(self):
        return SimpleNamespace(id="thread-1")

    def _thread_delete(self, thread_id):
        self.deleted.append(thread_id)
        if self._fail_delete:
            raise RuntimeError("delete failed")

    def _message_create(self, **_kwargs):
        if self._fail_message_create:
            raise RuntimeError("cannot post message")

    def _message_list(self, thread_id):
        self.messages_listed = True
        annotation = SimpleNamespace(url="https://example.com/source")
        block = SimpleNamespace(text=SimpleNamespace(value=self._reply, annotations=[annotation]))
        return [
            SimpleNamespace(role=MessageRole.USER, content=[]),
            SimpleNamespace(role=MessageRole.AGENT, content=[block]),
        ]

    def _next_status(self):
        # Advance through the scripted statuses, repeating the last one forever.
        return self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]

    def _run_create(self, thread_id, agent_id):
        return self._run(self._next_status())

    def _run_get(self, thread_id, run_id):
        return self._run(self._next_status())

    def _run_cancel(self, thread_id, run_id):
        self.cancelled.append(run_id)


@pytest.fixture
def install_client(monkeypatch):
    def _install(client, timeout=600):
        monkeypatch.setattr(foundry_agent, "_get_agents_client", lambda: client)
        monkeypatch.setattr(foundry_agent, "get_settings", lambda: SimpleNamespace(run_timeout_seconds=timeout))
        monkeypatch.setattr(foundry_agent.time, "sleep", lambda _seconds: None)

    return _install


def test_completed_run_returns_reply_and_deletes_thread(install_client):
    client = FakeAgentsClient(["queued", "in_progress", "completed"])
    install_client(client)

    result = foundry_agent.run_agent_prompt("agent-1", "question")

    assert result.completed
    assert result.text == "Hello"
    assert result.has_response
    assert result.annotation_urls == {"https://example.com/source"}
    assert result.run_id == "run-1"
    assert client.deleted == ["thread-1"]


def test_failed_run_reports_status_without_reading_messages(install_client):
    client = FakeAgentsClient(["failed"])
    install_client(client)

    result = foundry_agent.run_agent_prompt("agent-1", "question")

    assert not result.completed
    assert result.status == "failed"
    assert result.last_error == "boom"
    assert result.text == ""
    assert not client.messages_listed
    assert client.deleted == ["thread-1"]


def test_run_that_never_finishes_is_cancelled_after_timeout(install_client, monkeypatch):
    client = FakeAgentsClient(["in_progress"])
    install_client(client, timeout=300)
    # Fake clock: every reading advances 100 seconds.
    ticks = itertools.count(0, 100)
    monkeypatch.setattr(foundry_agent.time, "monotonic", lambda: next(ticks))

    result = foundry_agent.run_agent_prompt("agent-1", "question")

    assert result.status == "timed_out"
    assert not result.completed
    assert client.cancelled == ["run-1"]
    assert client.deleted == ["thread-1"]


def test_thread_is_deleted_when_posting_the_message_fails(install_client):
    client = FakeAgentsClient(["completed"], fail_message_create=True)
    install_client(client)

    with pytest.raises(RuntimeError, match="cannot post message"):
        foundry_agent.run_agent_prompt("agent-1", "question")

    assert client.deleted == ["thread-1"]


def test_thread_delete_failure_does_not_fail_the_request(install_client):
    client = FakeAgentsClient(["completed"], fail_delete=True)
    install_client(client)

    result = foundry_agent.run_agent_prompt("agent-1", "question")

    assert result.completed
    assert result.text == "Hello"
    assert client.deleted == ["thread-1"]
