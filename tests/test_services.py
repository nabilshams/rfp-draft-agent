"""Shared Azure clients, settings-derived endpoints, KB chunk deletion, ingestion, template endpoints and startup."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import azure_clients, main
from app.agents import lifecycle
from app.config import Settings, get_settings
from app.ingestion import kb_ingest
from app.routers import template
from app.services import kb_access

from .test_authorization_api import ADMIN, SAMPLE_DOCX, A, make_settings


def settings(**overrides) -> Settings:
    return make_settings(**overrides)


def endpoint_settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        auth_mode="disabled",
        azure_ai_search_connection_name="search",
        azure_storage_account_url="https://example.blob.core.windows.net",
        **{"foundry_project_endpoint": "https://example.services.ai.azure.com", **overrides},
    )


# ── Derived endpoints ────────────────────────────────────


class TestSettingsEndpoints:
    def test_the_search_endpoint_comes_from_the_connection_name_unless_given(self):
        assert settings().search_endpoint == "https://search.search.windows.net"
        assert settings(azure_ai_search_endpoint="https://custom.example").search_endpoint == "https://custom.example"

    @pytest.mark.parametrize(
        ("endpoint", "project", "expected"),
        [
            ("https://a.services.ai.azure.com", "", "https://a.services.ai.azure.com"),
            ("https://a.services.ai.azure.com/", "p1", "https://a.services.ai.azure.com/api/projects/p1"),
            ("https://a.services.ai.azure.com/openai", "p1", "https://a.services.ai.azure.com/api/projects/p1"),
        ],
    )
    def test_the_agents_endpoint_is_project_scoped(self, endpoint, project, expected):
        assert (
            endpoint_settings(foundry_project_endpoint=endpoint, foundry_project_name=project).agents_endpoint
            == expected
        )

    @pytest.mark.parametrize(
        ("endpoint", "expected"),
        [
            ("https://a.services.ai.azure.com/api/projects/p", "https://a.openai.azure.com"),
            ("https://a.cognitiveservices.azure.com/", "https://a.openai.azure.com"),
            ("https://a.openai.azure.com", "https://a.openai.azure.com"),
        ],
    )
    def test_the_openai_endpoint_is_derived_from_the_foundry_endpoint(self, endpoint, expected):
        assert endpoint_settings(foundry_project_endpoint=endpoint).openai_endpoint == expected


# ── Shared clients ───────────────────────────────────────


class TestAzureClients:
    @pytest.fixture(autouse=True)
    def clean(self, monkeypatch):
        azure_clients.reset()
        created = SimpleNamespace(items=[])

        def fake(kind):
            def factory(*args, **kwargs):
                obj = SimpleNamespace(kind=kind, args=args, kwargs=kwargs)
                obj.get_container_client = lambda name: SimpleNamespace(kind="container", name=name)
                created.items.append(obj)
                return obj

            return factory

        monkeypatch.setattr("azure.identity.DefaultAzureCredential", fake("credential"))
        monkeypatch.setattr("azure.identity.get_bearer_token_provider", lambda *_a, **_k: "token-provider")
        monkeypatch.setattr("azure.storage.blob.BlobServiceClient", fake("blob"))
        monkeypatch.setattr("azure.search.documents.SearchClient", fake("search"))
        monkeypatch.setattr("azure.search.documents.indexes.SearchIndexClient", fake("index"))
        monkeypatch.setattr("azure.ai.agents.AgentsClient", fake("agents"))
        monkeypatch.setattr("openai.AzureOpenAI", fake("openai"))
        self.created = created
        yield
        azure_clients.reset()

    def test_a_client_is_created_once_and_reused(self):
        s = settings()
        assert azure_clients.blob_service(s) is azure_clients.blob_service(s)
        assert [c.kind for c in self.created.items].count("blob") == 1

    def test_every_client_shares_one_credential(self):
        s = settings()
        azure_clients.blob_service(s)
        azure_clients.search_client(s)
        azure_clients.search_index_client(s)
        azure_clients.agents_client(s)
        azure_clients.openai_client(s)
        assert [c.kind for c in self.created.items].count("credential") == 1

    def test_clients_use_the_configured_endpoints(self):
        s = settings(foundry_project_name="proj")
        assert azure_clients.blob_service(s).kwargs["account_url"] == "https://example.blob.core.windows.net"
        search = azure_clients.search_client(s)
        assert search.kwargs["endpoint"] == "https://search.search.windows.net"
        assert search.kwargs["index_name"] == "rfp-knowledge-base"
        assert azure_clients.search_index_client(s).kwargs["endpoint"] == "https://search.search.windows.net"
        assert azure_clients.agents_client(s).kwargs["endpoint"] == "https://example.services.ai.azure.com/api/projects/proj"
        openai = azure_clients.openai_client(s)
        assert openai.kwargs["azure_endpoint"] == "https://example.openai.azure.com"
        assert openai.kwargs["azure_ad_token_provider"] == "token-provider"

    def test_containers_come_from_the_shared_blob_service(self):
        s = settings()
        assert azure_clients.container(s, "outputs").name == "outputs"
        assert azure_clients.container(s, "kb").name == "kb"
        assert [c.kind for c in self.created.items].count("blob") == 1

    def test_reset_forgets_everything(self):
        s = settings()
        first = azure_clients.blob_service(s)
        azure_clients.reset()
        assert azure_clients.blob_service(s) is not first


# ── Removing a document's search chunks ──────────────────


class TestDeleteKbChunks:
    class Index:
        def __init__(self, search_fails=False, delete_fails=0):
            self.search_fails, self.delete_fails = search_fails, delete_fails
            self.deleted = []
            self.filters = []

        def search(self, **kwargs):
            self.filters.append(kwargs["filter"])
            if self.search_fails:
                raise RuntimeError("index unavailable")
            return [{"id": "c1"}, {"id": "c2"}]

        def delete_documents(self, documents):
            if self.delete_fails:
                self.delete_fails -= 1
                raise RuntimeError("delete failed")
            self.deleted.extend(documents)

    @pytest.fixture
    def index(self, monkeypatch):
        def use(**kwargs):
            index = self.Index(**kwargs)
            monkeypatch.setattr(azure_clients, "search_client", lambda _s: index)
            return index

        return use

    def test_chunks_found_by_source_url_are_removed(self, index):
        idx = index()
        assert kb_access.delete_kb_chunks(settings(), "acme/p1/general/it's.pdf") == 2
        assert idx.deleted == [{"id": "c1"}, {"id": "c2"}]
        assert "it''s.pdf" in idx.filters[0]  # quotes are escaped in the OData filter

    def test_if_the_search_fails_the_predictable_chunk_ids_are_deleted(self, index):
        idx = index(search_fails=True)
        assert kb_access.delete_kb_chunks(settings(), "acme/p1/general/a.pdf") == 200
        assert len(idx.deleted) == 200

    def test_if_everything_fails_nothing_is_reported_deleted(self, index):
        index(search_fails=True, delete_fails=5)
        assert kb_access.delete_kb_chunks(settings(), "acme/p1/general/a.pdf") == 0


# ── Ingestion ────────────────────────────────────────────


class WordTokenizer:
    """Stands in for tiktoken (which may need a download): one token per word."""

    def encode(self, text):
        return text.split()

    def decode(self, tokens):
        return " ".join(tokens)


class TestIngestBlob:
    @pytest.fixture
    def pipeline(self, monkeypatch):
        state = SimpleNamespace(uploaded=[], embedded=[])
        monkeypatch.setattr(kb_ingest, "_tokenizer", lambda: WordTokenizer())
        monkeypatch.setattr(kb_ingest, "_ensure_index_once", lambda: None)
        monkeypatch.setattr(
            azure_clients,
            "container",
            lambda _s, _n: SimpleNamespace(download_blob=lambda _b: SimpleNamespace(readall=lambda: SAMPLE_DOCX)),
        )

        def embed(texts):
            state.embedded.append(list(texts))
            return [[0.1, 0.2]] * len(texts)

        monkeypatch.setattr(kb_ingest, "_embed_texts", embed)
        monkeypatch.setattr(
            azure_clients, "search_client", lambda _s: SimpleNamespace(upload_documents=state.uploaded.extend)
        )
        monkeypatch.setattr(kb_ingest, "get_settings", lambda: settings())
        return state

    def test_the_document_is_chunked_embedded_and_indexed_with_its_access_rights(self, pipeline):
        count = kb_ingest.ingest_blob(
            "acme/p1/general/guide.docx", "acme", "p1", "general", oids=["owner-1"], groups=["team-1"]
        )

        assert count == len(pipeline.uploaded) > 0
        doc = pipeline.uploaded[0]
        assert doc["client_id"] == "acme" and doc["project_id"] == "p1" and doc["solution_area"] == "general"
        assert doc["oids"] == ["owner-1"] and doc["groups"] == ["team-1"]
        assert doc["title"] == "guide.docx"
        assert doc["source_url"] == "https://example.blob.core.windows.net/knowledge-base/acme/p1/general/guide.docx"
        assert doc["contentVector"] == [0.1, 0.2]
        assert len({d["id"] for d in pipeline.uploaded}) == count  # stable, unique chunk ids

    def test_ids_are_stable_so_reindexing_replaces_chunks(self, pipeline):
        kb_ingest.ingest_blob("acme/p1/general/guide.docx", "acme", "p1", "general")
        first = [d["id"] for d in pipeline.uploaded]
        pipeline.uploaded.clear()
        kb_ingest.ingest_blob("acme/p1/general/guide.docx", "acme", "p1", "general")
        assert [d["id"] for d in pipeline.uploaded] == first

    def test_a_document_without_text_indexes_nothing(self, pipeline, monkeypatch):
        monkeypatch.setattr(kb_ingest, "extract_text", lambda *_a: "   ")
        assert kb_ingest.ingest_blob("acme/p1/general/empty.docx", "acme", "p1", "general") == 0
        assert pipeline.uploaded == []

    def test_long_sections_are_split_into_overlapping_chunks(self, monkeypatch):
        monkeypatch.setattr(kb_ingest, "_tokenizer", lambda: WordTokenizer())
        words = [f"w{i}" for i in range(25)]
        chunks = kb_ingest._chunk_text(" ".join(words), max_tokens=10, overlap_tokens=2)
        assert chunks[0].split() == words[:10]
        assert chunks[1].split()[:2] == words[8:10]  # overlap
        assert " ".join(chunks).count("w24") >= 1

    def test_embeddings_use_the_configured_deployment(self, monkeypatch):
        calls = []
        fake = SimpleNamespace(
            embeddings=SimpleNamespace(
                create=lambda **kw: calls.append(kw) or SimpleNamespace(data=[SimpleNamespace(embedding=[1.0])])
            )
        )
        monkeypatch.setattr(azure_clients, "openai_client", lambda _s: fake)
        monkeypatch.setattr(kb_ingest, "get_settings", lambda: settings(embedding_model_deployment_name="emb-1"))
        assert kb_ingest._embed_texts(["hello"]) == [[1.0]]
        assert calls == [{"model": "emb-1", "input": ["hello"]}]


# ── Template endpoints ───────────────────────────────────


class TestTemplate:
    @pytest.fixture
    def template_path(self, tmp_path, monkeypatch):
        path = tmp_path / "templates" / "rfp_response_template.docx"
        monkeypatch.setattr(template, "TEMPLATE_PATH", path)
        return path

    async def test_info_reports_when_there_is_no_template(self, client, template_path):
        response = await client.get("/template/info", headers=A)
        assert response.json() == {"exists": False, "sections": []}

    async def test_info_describes_the_uploaded_template(self, client, template_path):
        await client.post("/template/upload", files={"file": ("t.docx", SAMPLE_DOCX)}, headers=ADMIN)
        info = (await client.get("/template/info", headers=A)).json()
        assert info["exists"] is True
        assert info["size_bytes"] == len(SAMPLE_DOCX)
        assert "last_modified" in info

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            (bytes.fromhex("D0CF11E0A1B11AE1") + b"rest", "old .doc"),
            (b"plain text", "does not appear to be a valid Word document"),
        ],
    )
    async def test_files_that_are_not_docx_are_saved_with_a_warning(self, client, template_path, data, expected):
        response = await client.post("/template/upload", files={"file": ("t.docx", data)}, headers=ADMIN)
        assert response.status_code == 200
        assert expected in response.json()["warning"]

    async def test_a_valid_docx_has_no_warning(self, client, template_path):
        response = await client.post("/template/upload", files={"file": ("t.docx", SAMPLE_DOCX)}, headers=ADMIN)
        assert "warning" not in response.json()


# ── Startup ──────────────────────────────────────────────


class TestStartup:
    def test_the_app_starts_even_if_agent_setup_fails(self, monkeypatch, caplog):
        def failing_setup():
            raise RuntimeError("Foundry unreachable")

        monkeypatch.setattr(lifecycle, "setup_agents", failing_setup)
        monkeypatch.setattr("app.telemetry.tracing.configure_tracing", lambda _app: None)

        with TestClient(main.app) as test_client:
            assert test_client.get("/health").status_code == 200

        assert any("Agent setup deferred" in r.message for r in caplog.records)

    def test_the_root_page_serves_the_ui(self):
        response = TestClient(main.app).get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


class TestRun:
    @pytest.fixture
    def uvicorn_calls(self, monkeypatch):
        calls = []
        monkeypatch.setattr("uvicorn.run", lambda *a, **kw: calls.append((a, kw)))
        return calls

    def run_with(self, monkeypatch, **overrides):
        monkeypatch.setattr(main, "get_settings", lambda: settings(**overrides))
        main.run()

    def test_it_starts_uvicorn_with_the_configured_host_port_and_workers(self, monkeypatch, uvicorn_calls):
        self.run_with(monkeypatch, host="127.0.0.1", port=9000, parallelism=3, log_level="DEBUG")
        (args, kwargs) = uvicorn_calls[0]
        assert args == ("app.main:app",)
        assert (kwargs["host"], kwargs["port"], kwargs["workers"], kwargs["log_level"]) == ("127.0.0.1", 9000, 3, "debug")
        assert kwargs["reload"] is False

    def test_reload_forces_a_single_worker(self, monkeypatch, uvicorn_calls):
        self.run_with(monkeypatch, uvicorn_reload=True, parallelism=4)
        assert uvicorn_calls[0][1]["workers"] == 1
        assert uvicorn_calls[0][1]["reload"] is True


def test_the_settings_dependency_is_overridable_for_health(use_settings):  # noqa: F811
    async def check():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://t") as c:
            return await c.get("/health")

    import asyncio

    assert asyncio.run(check()).status_code == 200
    assert get_settings in main.app.dependency_overrides
