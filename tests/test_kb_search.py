"""Tests for hybrid knowledge-base search, query embedding, context building and citation checks."""

from types import SimpleNamespace

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.search.documents.models import VectorFilterMode

from app import azure_clients
from app.agents import kb_search
from app.config import Settings

ACCESS = {
    "client_id": "acme",
    "project_id": "p1",
    "solution_area": "cloud",
    "user_oid": "user-a",
    "user_groups": ["team-1"],
}


def make_settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        auth_mode="disabled",
        foundry_project_endpoint="https://example.services.ai.azure.com",
        azure_ai_search_connection_name="search",
        azure_storage_account_url="https://example.blob.core.windows.net",
        **overrides,
    )


class FakeSearchClient:
    """Records every search() call; can be told to fail the first few."""

    def __init__(self, results=(), errors=()):
        self.results = list(results)
        self.errors = list(errors)
        self.calls: list[dict] = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return iter(self.results)


@pytest.fixture
def search(monkeypatch):
    def _install(client, **settings):
        monkeypatch.setattr(azure_clients, "search_client", lambda _settings: client)
        monkeypatch.setattr(kb_search, "get_settings", lambda: make_settings(**settings))
        return client

    return _install


def hit(title="Guide", url="https://kb/acme/p1/cloud/guide.pdf", content="text", score=1.0, reranker=None):
    return {
        "title": title,
        "source_url": url,
        "content": content,
        "@search.score": score,
        "@search.reranker_score": reranker,
    }


class TestSearchKb:
    def test_hybrid_query_carries_the_vector_and_the_access_filter(self, search):
        client = search(FakeSearchClient([hit()]))

        docs = kb_search.search_kb(query="encryption at rest", vector=[0.1, 0.2], **ACCESS)

        (call,) = client.calls
        assert call["search_text"] == "encryption at rest"
        assert call["top"] == 15  # top-k (5) widened by the candidate factor (3), then cut back after re-ranking
        assert "query_type" not in call
        (vector_query,) = call["vector_queries"]
        assert vector_query.vector == [0.1, 0.2]
        assert vector_query.fields == "contentVector"
        # The neighbour count must really reach the SDK model (a wrong parameter name is silently dropped).
        neighbours = getattr(vector_query, "k", None) or getattr(vector_query, "k_nearest_neighbors", None)
        assert neighbours == 30
        # Access filters must apply before the nearest-neighbour cut-off, not after it.
        assert call["vector_filter_mode"] == VectorFilterMode.PRE_FILTER
        assert "client_id eq 'acme'" in call["filter"]
        assert "oids/any(o: o eq 'user-a')" in call["filter"]
        assert docs[0]["title"] == "Guide"

    def test_without_a_vector_it_is_a_plain_keyword_search(self, search):
        client = search(FakeSearchClient([hit()]))
        kb_search.search_kb(query="q", vector=None, **ACCESS)
        (call,) = client.calls
        assert "vector_queries" not in call and "vector_filter_mode" not in call

    def test_semantic_ranker_is_opt_in(self, search):
        off = search(FakeSearchClient())
        kb_search.search_kb(query="q", **ACCESS)
        assert "query_type" not in off.calls[0]

        on = search(FakeSearchClient(), search_semantic_ranker=True)
        kb_search.search_kb(query="q", **ACCESS)
        assert on.calls[0]["query_type"] == "semantic"
        assert on.calls[0]["semantic_configuration_name"] == "default"

    def test_top_k_comes_from_settings_unless_overridden(self, search):
        client = search(FakeSearchClient(), search_top_k=8)
        kb_search.search_kb(query="q", **ACCESS)
        kb_search.search_kb(query="q", top_k=3, **ACCESS)
        assert [c["top"] for c in client.calls] == [24, 9]

    def test_results_are_mapped_to_plain_dicts(self, search):
        search(FakeSearchClient([hit(score=0.5, reranker=2.5)]))
        (doc,) = kb_search.search_kb(query="q", **ACCESS)
        assert doc == {
            "content": "text",
            "title": "Guide",
            "source_url": "https://kb/acme/p1/cloud/guide.pdf",
            "score": 0.5,
            "reranker_score": 2.5,
            # a chunk indexed before provenance existed: no type, no date; neutral weight (no tier, middling freshness)
            "source_type": "",
            "authority": "",
            "effective_date": "",
            "version": "",
            "approval_status": "",
            "section": "",
            "page": None,
            "blob_name": "",  # not a knowledge-base blob URL, so no reference link
            "weight": 0.85,
            "rank_score": 2.125,
        }

    def test_retrieved_chunks_are_redacted_but_their_urls_are_not(self, search):
        # Chunks were redacted at ingestion, but rules and customer names added since must still apply.
        url = "https://kb/acme/p1/cloud/guide.pdf"
        search(FakeSearchClient([hit(title="Pricing", url=url, content="We charge $1,250 per seat; call 555-123-4567.")]))

        (doc,) = kb_search.search_kb(query="q", **ACCESS)

        assert doc["content"] == "We charge [REDACTED-PRICE] per seat; call [REDACTED-PHONE]."
        assert doc["source_url"] == url  # must stay exact so the citation check still recognises it

    def test_the_minimum_score_uses_the_reranker_score_when_there_is_one(self, search):
        results = [
            hit(title="strong", score=0.01, reranker=3.1),
            hit(title="weak", score=0.9, reranker=0.4),  # high search score, low reranker score: dropped
            hit(title="no reranker, high", score=1.5),
            hit(title="no reranker, low", score=0.2),
        ]
        search(FakeSearchClient(results), search_min_score=1.0)
        titles = [d["title"] for d in kb_search.search_kb(query="q", **ACCESS)]
        assert titles == ["strong", "no reranker, high"]

    def test_a_missing_index_means_an_empty_knowledge_base(self, search):
        search(FakeSearchClient(errors=[ResourceNotFoundError("no index")]))
        assert kb_search.search_kb(query="q", **ACCESS) == []


class TestDegradation:
    def test_a_failing_semantic_ranker_is_dropped_but_the_vector_query_is_kept(self, search):
        client = search(
            FakeSearchClient([hit()], errors=[HttpResponseError(message="semantic unsupported")]),
            search_semantic_ranker=True,
        )

        docs = kb_search.search_kb(query="q", vector=[0.1], **ACCESS)

        assert len(docs) == 1
        first, second = client.calls
        assert "query_type" in first and "query_type" not in second
        assert "vector_queries" in second

    def test_a_failing_vector_query_falls_back_to_keyword_search(self, search):
        client = search(FakeSearchClient([hit()], errors=[HttpResponseError(message="dimension mismatch")]))

        docs = kb_search.search_kb(query="q", vector=[0.1], **ACCESS)

        assert len(docs) == 1
        assert "vector_queries" in client.calls[0]
        assert "vector_queries" not in client.calls[1]

    def test_both_fall_away_one_after_the_other(self, search):
        client = search(
            FakeSearchClient([hit()], errors=[HttpResponseError(message="a"), HttpResponseError(message="b")]),
            search_semantic_ranker=True,
        )
        kb_search.search_kb(query="q", vector=[0.1], **ACCESS)
        assert len(client.calls) == 3
        assert "query_type" not in client.calls[2] and "vector_queries" not in client.calls[2]

    def test_a_failing_keyword_search_is_raised(self, search):
        search(FakeSearchClient(errors=[HttpResponseError(message="service down")]))
        with pytest.raises(HttpResponseError):
            kb_search.search_kb(query="q", **ACCESS)


class TestEmbedQueries:
    @pytest.fixture
    def embedded(self, monkeypatch):
        calls = []

        def fake_embed(texts):
            calls.append(list(texts))
            return [[float(len(t))] for t in texts]

        monkeypatch.setattr("app.ingestion.kb_ingest.embed_texts", fake_embed)
        return calls

    def test_no_queries_means_no_call(self, embedded):
        assert kb_search.embed_queries([]) == []
        assert embedded == []

    def test_queries_are_batched_and_keep_their_order(self, embedded):
        queries = [f"q{'x' * i}" for i in range(40)]

        vectors = kb_search.embed_queries(queries)

        assert [len(batch) for batch in embedded] == [16, 16, 8]
        assert vectors == [[float(len(q))] for q in queries]

    def test_overlong_queries_are_truncated(self, embedded):
        kb_search.embed_queries(["x" * 5000])
        assert len(embedded[0][0]) == 2000

    def test_failure_degrades_to_no_vectors_instead_of_failing(self, monkeypatch):
        def broken(texts):
            raise RuntimeError("embedding deployment not found")

        monkeypatch.setattr("app.ingestion.kb_ingest.embed_texts", broken)
        assert kb_search.embed_queries(["a", "b"]) == [None, None]


class TestMergeAndFormat:
    def test_documents_are_taken_in_turns_so_every_query_gets_a_share(self):
        first = [{"source_url": "u1", "content": "a1"}, {"source_url": "u2", "content": "a2"}]
        second = [{"source_url": "u3", "content": "b1"}, {"source_url": "u4", "content": "b2"}]

        merged = kb_search.merge_docs([first, second], limit=3)

        assert [d["source_url"] for d in merged] == ["u1", "u3", "u2"]

    def test_duplicates_are_dropped(self):
        doc = {"source_url": "https://kb/a%20b.pdf", "content": "same chunk"}
        same = {"source_url": "https://kb/a b.pdf/", "content": "same chunk"}  # same URL, written differently

        assert kb_search.merge_docs([[doc], [same]], limit=10) == [doc]

    def test_an_empty_result_is_stated_explicitly(self):
        block = kb_search.format_context([])
        assert "No matching documents were found" in block
        assert "Do not cite or invent internal sources" in block

    def test_sources_are_listed_with_their_urls_and_truncated_content(self):
        block = kb_search.format_context([{"title": "Guide", "source_url": "https://kb/g.pdf", "content": "x" * 3000}])
        assert "### KB Source 1" in block
        assert "Title: Guide" in block
        assert "URL: https://kb/g.pdf" in block
        assert block.count("x") == 1500

    def test_retrieved_context_exposes_the_citable_urls(self):
        context = kb_search.RetrievedContext([{"source_url": "https://kb/a.pdf"}, {"source_url": ""}, {}])
        assert context.urls == {"https://kb/a.pdf"}
        assert context.found
        assert not kb_search.RetrievedContext().found


class TestRequirementRetriever:
    @pytest.fixture
    def kb(self, monkeypatch):
        state = SimpleNamespace(queries=[], embedded=[], fail_on=set())

        def fake_embed(queries):
            state.embedded.append(list(queries))
            return [[0.5]] * len(queries)

        def fake_search(*, query, vector=None, **access):
            state.queries.append((query, vector, access["user_oid"]))
            if query in state.fail_on:
                raise HttpResponseError(message="search unavailable")
            return [{"title": query, "source_url": f"https://kb/{query}.pdf", "content": query}]

        monkeypatch.setattr(kb_search, "embed_queries", fake_embed)
        monkeypatch.setattr(kb_search, "search_kb", fake_search)
        return state

    REQS = [{"id": "R1", "text": "alpha"}, {"id": "R2", "text": "beta"}, {"id": "R3", "text": "gamma"}]

    def retriever(self):
        return kb_search.RequirementRetriever(self.REQS, **ACCESS)

    def test_each_requirement_is_searched_with_its_own_text(self, kb):
        contexts = kb_search.retrieve_for_requirements(self.REQS, **ACCESS)

        assert [q[0] for q in kb.queries] == ["alpha", "beta", "gamma"]
        assert [c.docs[0]["title"] for c in contexts] == ["alpha", "beta", "gamma"]
        assert all(q[2] == "user-a" for q in kb.queries)  # searches run as the caller

    def test_all_queries_are_embedded_in_one_batch_up_front(self, kb):
        retriever = self.retriever()
        assert kb.embedded == [["alpha", "beta", "gamma"]]
        assert kb.queries == []  # searches only happen when asked for
        retriever.context(1)
        assert [q[0] for q in kb.queries] == ["beta"]

    def test_a_failed_search_leaves_that_requirement_without_evidence(self, kb):
        kb.fail_on = {"beta"}
        retriever = self.retriever()

        contexts = [retriever.context(i) for i in range(3)]

        assert [c.found for c in contexts] == [True, False, True]

    def test_repeated_failures_are_treated_as_systemic_and_raised(self, kb):
        kb.fail_on = {"alpha", "beta", "gamma"}
        retriever = self.retriever()

        assert not retriever.context(0).found
        assert not retriever.context(1).found
        with pytest.raises(HttpResponseError):
            retriever.context(2)

    def test_merged_contexts_are_capped(self, kb, monkeypatch):
        monkeypatch.setattr(kb_search, "get_settings", lambda: make_settings(search_max_context_docs=2))
        contexts = kb_search.retrieve_for_requirements(self.REQS, **ACCESS)
        merged = kb_search.merge_contexts(contexts)
        assert [d["title"] for d in merged.docs] == ["alpha", "beta"]


class TestSanitizeCitations:
    KB = "https://kb.blob.core.windows.net/knowledge-base/acme/p1/general/guide.pdf"

    def clean(self, value, *allowed):
        return kb_search.sanitize_citations(value, allowed)

    def test_links_to_retrieved_sources_are_kept(self):
        text = f"See [the guide]({self.KB}) for details."
        assert self.clean(text, self.KB) == text

    def test_links_to_anything_else_become_plain_text(self):
        text = "See [the guide](https://evil.example/guide) and [docs](https://learn.microsoft.com/x)."
        assert self.clean(text, self.KB) == "See the guide and docs."

    def test_in_page_anchors_are_left_alone(self):
        assert self.clean("Jump to [section](#details)") == "Jump to [section](#details)"

    def test_web_grounding_urls_can_be_allowed(self):
        text = "[Azure SLA](https://azure.microsoft.com/support/legal/sla/)"
        assert self.clean(text, "https://azure.microsoft.com/support/legal/sla/") == text

    def test_urls_match_regardless_of_trailing_slash_and_percent_encoding(self):
        encoded = "https://kb.blob.core.windows.net/knowledge-base/acme/p1/general/My%20Guide.pdf"
        plain = "https://kb.blob.core.windows.net/knowledge-base/acme/p1/general/My Guide.pdf"
        assert self.clean(f"[g]({encoded})", plain) == f"[g]({encoded})"
        assert self.clean("[a](https://x.example/a/)", "https://x.example/a") == "[a](https://x.example/a/)"

    def test_a_link_cut_at_a_parenthesis_in_the_file_name_is_not_mistaken_for_a_fabrication(self):
        known = "https://kb.blob.core.windows.net/knowledge-base/acme/p1/general/Guide (final).pdf"
        truncated = "https://kb.blob.core.windows.net/knowledge-base/acme/p1/general/Guide (final"
        assert self.clean(f"[g]({truncated})", known) == f"[g]({truncated})"

    def test_a_bare_host_is_not_accepted_as_a_prefix_of_a_real_source(self):
        assert self.clean("[x](https://kb.blob.core.windows.net)", self.KB) == "x"
        assert self.clean("[x](https)", self.KB) == "x"

    def test_evidence_must_cite_a_retrieved_source(self):
        result = {
            "answer_markdown": "ok",
            "evidence": [
                {"title": "Guide", "source_url": self.KB, "quote": "..."},
                {"title": "Invented", "source_url": "https://evil.example/doc", "quote": "..."},
                {"title": "No URL", "source_url": "", "quote": "..."},
                {"title": "Missing"},
                "not a dict",
            ],
        }

        cleaned = self.clean(result, self.KB)

        assert [e["title"] for e in cleaned["evidence"]] == ["Guide"]

    def test_nested_results_are_cleaned_and_the_input_is_not_modified(self):
        result = {
            "executive_summary": f"[a](https://evil.example) and [b]({self.KB})",
            "responses": [{"answer_markdown": "[c](https://evil.example)", "evidence": [], "n": 3}],
        }

        cleaned = self.clean(result, self.KB)

        assert cleaned["executive_summary"] == f"a and [b]({self.KB})"
        assert cleaned["responses"][0] == {"answer_markdown": "c", "evidence": [], "n": 3}
        assert result["responses"][0]["answer_markdown"] == "[c](https://evil.example)"

    def test_cited_urls_collects_links_and_evidence(self):
        value = [
            {"answer_markdown": f"[a]({self.KB}) and [h](#x)", "evidence": [{"source_url": "https://other.example/d"}]}
        ]
        assert kb_search.cited_urls(value) == {self.KB, "#x", "https://other.example/d"}
