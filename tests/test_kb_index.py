"""Tests for the search index definition and for creating it before the first ingest."""

from types import SimpleNamespace

import pytest

from app.ingestion import kb_ingest


class TestIndexSchema:
    schema = kb_ingest.get_index_schema("rfp-knowledge-base")

    def test_the_vector_field_matches_the_query_side(self):
        field = next(f for f in self.schema.fields if f.name == "contentVector")
        assert field.vector_search_dimensions == 1536  # text-embedding-3-small
        assert field.vector_search_profile_name == "default-profile"
        assert [p.name for p in self.schema.vector_search.profiles] == ["default-profile"]

    def test_the_semantic_configuration_that_queries_ask_for_exists(self):
        semantic = self.schema.semantic_search
        assert semantic.default_configuration_name == "default"
        (config,) = semantic.configurations
        assert config.name == "default"
        assert config.prioritized_fields.title_field.field_name == "title"
        assert [f.field_name for f in config.prioritized_fields.content_fields] == ["content"]

    def test_fields_used_for_access_control_are_filterable(self):
        filterable = {f.name for f in self.schema.fields if getattr(f, "filterable", False)}
        assert {"client_id", "project_id", "solution_area", "oids", "groups"} <= filterable


class TestEnsureIndexOnce:
    @pytest.fixture(autouse=True)
    def fresh_state(self, monkeypatch):
        monkeypatch.setattr(kb_ingest, "_index_ready", False)

    @pytest.fixture
    def index_client(self, monkeypatch):
        state = SimpleNamespace(updates=[], fail=False)

        class FakeIndexClient:
            def __init__(self, endpoint, credential):
                state.endpoint = endpoint

            def create_or_update_index(self, index):
                if state.fail:
                    raise RuntimeError("authorization has not propagated yet")
                state.updates.append(index.name)

        monkeypatch.setattr(kb_ingest, "SearchIndexClient", FakeIndexClient)
        monkeypatch.setattr(kb_ingest, "DefaultAzureCredential", lambda: object())
        return state

    def test_the_index_is_created_or_updated_once_per_process(self, index_client):
        for _ in range(3):
            kb_ingest._ensure_index_once()
        assert index_client.updates == ["rfp-knowledge-base"]

    def test_a_failure_does_not_block_ingestion_and_is_retried_next_time(self, index_client):
        index_client.fail = True
        kb_ingest._ensure_index_once()  # must not raise
        assert index_client.updates == []

        index_client.fail = False
        kb_ingest._ensure_index_once()
        assert index_client.updates == ["rfp-knowledge-base"]

    def test_ingest_makes_sure_the_index_exists_first(self, index_client, monkeypatch):
        order = []
        monkeypatch.setattr(kb_ingest, "_ensure_index_once", lambda: order.append("ensure"))

        class StopHere(Exception):
            pass

        def fake_blob_service(**kwargs):
            order.append("download")
            raise StopHere

        monkeypatch.setattr(kb_ingest, "BlobServiceClient", fake_blob_service)

        with pytest.raises(StopHere):
            kb_ingest.ingest_blob("acme/p1/general/guide.pdf", "acme", "p1", "general", oids=["u"])

        assert order == ["ensure", "download"]
