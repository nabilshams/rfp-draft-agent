"""Web grounding (Bing) is opt-in per request: agents carry no standing tool, and the API refuses it when unconfigured."""

from types import SimpleNamespace

import httpx
import pytest

from app.agents import foundry_agent, kb_search
from app.config import Settings, get_settings
from app.main import app

BING_ID = "/subscriptions/s/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/a/projects/p/connections/bing"
BING_TOOL = SimpleNamespace(type="bing_grounding")


class FakeClient:
    def __init__(self, agents=(), strip_works=True):
        self.agents = {a.id: a for a in agents}
        self.strip_works = strip_works
        self.created = []
        self.updated = []
        self.run_kwargs = None
        self.threads = SimpleNamespace(create=lambda: SimpleNamespace(id="t1"), delete=lambda _id: None)
        self.messages = SimpleNamespace(
            create=lambda **_kw: None,
            list=lambda thread_id: [
                SimpleNamespace(
                    role=foundry_agent.MessageRole.AGENT,
                    content=[SimpleNamespace(text=SimpleNamespace(value="hi", annotations=[]))],
                )
            ],
        )
        self.runs = SimpleNamespace(create=self._run_create)

    def _run_create(self, **kwargs):
        self.run_kwargs = kwargs
        return SimpleNamespace(id="r1", status="completed", last_error=None)

    def list_agents(self):
        return list(self.agents.values())

    def get_agent(self, agent_id):
        return self.agents[agent_id]

    def update_agent(self, agent_id, **kwargs):
        self.updated.append((agent_id, kwargs))
        if "tools" in kwargs and self.strip_works:
            self.agents[agent_id].tools = kwargs["tools"]

    def create_agent(self, **kwargs):
        self.created.append(kwargs)
        agent = SimpleNamespace(id=f"new-{len(self.created)}", name=kwargs["name"], tools=kwargs.get("tools", []))
        self.agents[agent.id] = agent
        return agent


@pytest.fixture
def configured(monkeypatch):
    def _configure(bing=BING_ID):
        settings = Settings(bing_connection_id=bing)
        monkeypatch.setattr(foundry_agent, "get_settings", lambda: settings)
        return settings

    return _configure


@pytest.fixture(autouse=True)
def fresh_agent_state(monkeypatch):
    monkeypatch.setattr(foundry_agent, "_draft_agent_id", None)
    monkeypatch.setattr(foundry_agent, "_qa_agent_id", None)
    monkeypatch.setattr(foundry_agent, "_current_instructions_hash", None)
    monkeypatch.setattr(foundry_agent.time, "sleep", lambda _s: None)


class TestRunTools:
    def test_a_plain_run_attaches_no_tools(self, monkeypatch, configured):
        configured()
        client = FakeClient()
        monkeypatch.setattr(foundry_agent, "_get_agents_client", lambda: client)

        foundry_agent.run_agent_prompt("a1", "q")

        assert "tools" not in client.run_kwargs

    def test_a_web_search_run_attaches_bing_to_that_run_only(self, monkeypatch, configured):
        configured()
        client = FakeClient()
        monkeypatch.setattr(foundry_agent, "_get_agents_client", lambda: client)

        foundry_agent.run_agent_prompt("a1", "q", web_search=True)

        (tool,) = client.run_kwargs["tools"]
        assert tool["type"] == "bing_grounding"
        assert tool["bing_grounding"]["search_configurations"][0]["connection_id"] == BING_ID

    def test_web_search_without_a_connection_is_refused_before_anything_is_sent(self, monkeypatch, configured):
        configured(bing="")
        client = FakeClient()
        monkeypatch.setattr(foundry_agent, "_get_agents_client", lambda: client)

        with pytest.raises(foundry_agent.WebSearchNotConfigured):
            foundry_agent.run_agent_prompt("a1", "q", web_search=True)

        assert client.run_kwargs is None


class TestAgentsCarryNoStandingTool:
    def test_new_agents_are_created_without_tools_even_when_bing_is_configured(self, monkeypatch, configured):
        configured()
        client = FakeClient()
        monkeypatch.setattr(foundry_agent, "_get_agents_client", lambda: client)

        foundry_agent._ensure_agent(foundry_agent._DRAFT_AGENT_NAME)

        (created,) = client.created
        assert "tools" not in created

    def test_bing_is_removed_from_an_agent_created_by_an_earlier_version(self, monkeypatch, configured):
        configured()
        old = SimpleNamespace(id="old", name=foundry_agent._DRAFT_AGENT_NAME, tools=[BING_TOOL])
        client = FakeClient([old])
        monkeypatch.setattr(foundry_agent, "_get_agents_client", lambda: client)

        assert foundry_agent._ensure_agent(foundry_agent._DRAFT_AGENT_NAME) == "old"

        assert old.tools == []
        assert ("old", {"tools": []}) in client.updated

    def test_it_fails_closed_if_bing_cannot_be_removed(self, monkeypatch, configured):
        configured()
        old = SimpleNamespace(id="old", name=foundry_agent._DRAFT_AGENT_NAME, tools=[BING_TOOL])
        client = FakeClient([old], strip_works=False)
        monkeypatch.setattr(foundry_agent, "_get_agents_client", lambda: client)

        with pytest.raises(RuntimeError, match="Bing"):
            foundry_agent._ensure_agent(foundry_agent._DRAFT_AGENT_NAME)

    def test_agents_without_tools_are_left_alone(self, monkeypatch, configured):
        configured()
        clean = SimpleNamespace(id="ok", name=foundry_agent._DRAFT_AGENT_NAME, tools=[])
        client = FakeClient([clean])
        monkeypatch.setattr(foundry_agent, "_get_agents_client", lambda: client)

        foundry_agent._ensure_agent(foundry_agent._DRAFT_AGENT_NAME)

        assert all("tools" not in kwargs for _, kwargs in client.updated)


class TestFlowsPassTheChoiceThrough:
    @pytest.fixture
    def seen(self, monkeypatch):
        flags = []

        def fake_run(agent_id, content, *, web_search=False):
            flags.append(web_search)
            return foundry_agent.AgentRunResult(status="completed", text='{"meets": "Yes"}', has_response=True)

        monkeypatch.setattr(foundry_agent, "run_agent_prompt", fake_run)
        monkeypatch.setattr(foundry_agent, "_ensure_agent", lambda name: "agent-1")
        monkeypatch.setattr(kb_search, "embed_queries", lambda qs: [[0.1]] * len(qs))
        monkeypatch.setattr(kb_search, "search_kb", lambda **_kw: [])
        return flags

    REQS = [{"id": "R1", "text": "encryption"}]

    @pytest.mark.parametrize("allowed", [False, True])
    def test_per_requirement(self, seen, allowed):
        list(
            foundry_agent.stream_per_requirement(
                requirements=self.REQS, client_id="c", project_id="p", user_oid="u", allow_web_search=allowed
            )
        )
        assert seen == [allowed]

    @pytest.mark.parametrize("allowed", [False, True])
    def test_single_answer(self, seen, allowed):
        foundry_agent.run_single_answer(
            question="q", client_id="c", project_id="p", user_oid="u", allow_web_search=allowed
        )
        assert seen == [allowed]

    def test_the_default_is_no_web_search(self, seen):
        foundry_agent.run_single_answer(question="q", client_id="c", project_id="p", user_oid="u")
        assert seen == [False]


class TestApi:
    BODY = {"question": "q", "client_id": "acme", "project_id": "p1", "solution_area": "cloud"}

    @pytest.fixture
    def calls(self, monkeypatch):
        calls = []
        monkeypatch.setattr(foundry_agent, "run_single_answer", lambda **kw: calls.append(kw) or {"answer_markdown": "ok"})
        return calls

    @pytest.fixture
    def post(self):
        async def _post(path, body, bing=""):
            settings = Settings(auth_mode="disabled", bing_connection_id=bing)
            app.dependency_overrides[get_settings] = lambda: settings
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                    return await c.post(path, json=body)
            finally:
                app.dependency_overrides.pop(get_settings, None)

        return _post

    async def test_web_search_is_off_by_default(self, calls, post):
        resp = await post("/answer", self.BODY, bing=BING_ID)
        assert resp.status_code == 200
        assert calls[0]["allow_web_search"] is False

    async def test_it_is_passed_on_when_requested_and_configured(self, calls, post):
        resp = await post("/answer", {**self.BODY, "allow_web_search": True}, bing=BING_ID)
        assert resp.status_code == 200
        assert calls[0]["allow_web_search"] is True

    async def test_requesting_it_when_unconfigured_is_a_400(self, calls, post):
        resp = await post("/answer", {**self.BODY, "allow_web_search": True}, bing="")
        assert resp.status_code == 400
        assert calls == []

    async def test_health_says_whether_it_is_available(self, monkeypatch):
        from app import main

        for bing, expected in ((BING_ID, True), ("", False)):
            monkeypatch.setattr(main, "get_settings", lambda bing=bing: Settings(bing_connection_id=bing))
            assert (await main.health())["web_search_available"] is expected
