"""Knowledge layer v2: source types, authority, real dates, chunk-level provenance, ranking, evidence objects."""

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from azure.core.exceptions import HttpResponseError

from app import azure_clients
from app.agents import answers, kb_search
from app.agents.kb_search import RetrievedContext
from app.ingestion import kb_ingest
from app.ingestion.provenance import (
    Provenance,
    ProvenanceError,
    RankingPolicy,
    build_provenance,
    describe_source,
    provenance_from_metadata,
)
from app.ingestion.redaction import get_redaction_engine

from .test_authorization_api import ADMIN, KB_BLOB, A, upload_kb
from .test_draft_endpoints import agent  # noqa: F401  (a fixture)
from .test_extraction_fidelity import make_pdf
from .test_kb_search import ACCESS, FakeSearchClient, hit, search  # noqa: F401  (search is a fixture)
from .test_services import TestIngestBlob, WordTokenizer, settings  # noqa: F401

TODAY = date(2026, 10, 22)
POLICY = RankingPolicy(
    tier_weights={"authoritative": 1.3, "approved_answer": 1.15, "historical": 1.0, "external": 0.9},
    status_weights={"approved": 1.0, "unreviewed": 1.0, "draft": 0.8, "superseded": 0.5},
    half_life_days=730,
    freshness_floor=0.7,
)


def sourced(title, url, score, content="text", **provenance):
    return {**hit(title=title, url=url, content=content, score=score), **provenance}


# ── Provenance values ────────────────────────────────────


class TestProvenance:
    def test_a_new_document_is_historical_and_unreviewed_and_dated_from_its_file(self):
        p = build_provenance(fallback_date=datetime(2025, 3, 1, tzinfo=UTC))
        assert (p.source_type, p.approval_status, p.effective_date) == ("historical", "unreviewed", "2025-03-01")

    def test_it_survives_a_round_trip_through_blob_metadata(self):
        p = build_provenance(
            source_type="authoritative", effective_date="2026-01-15", version="3.1", authority="Security & Compliance",
            owner="Priya N.", approval_status="approved",
        )
        meta = p.to_metadata()
        assert all(v.isascii() for v in meta.values())  # blob metadata must be plain ASCII
        assert provenance_from_metadata(meta) == p

    def test_non_ascii_text_is_safe_in_metadata(self):
        p = build_provenance(authority="Équipe sécurité")
        assert provenance_from_metadata(p.to_metadata()).authority == "Équipe sécurité"

    def test_documents_without_recorded_provenance_are_not_promoted(self):
        # An existing blob (uploaded before provenance existed), and Azure's None for a missing value
        legacy = provenance_from_metadata({"owner_oid": "u", "shared_groups": None}, datetime(2024, 5, 6, tzinfo=UTC))
        assert legacy == Provenance(effective_date="2024-05-06")

    def test_damaged_metadata_falls_back_to_defaults(self):
        p = provenance_from_metadata({"kb_source_type": "gold-standard", "kb_effective_date": "yesterday"})
        assert (p.source_type, p.effective_date) == ("historical", "")

    @pytest.mark.parametrize(
        "field, value",
        [
            ("source_type", "gold"),
            ("approval_status", "maybe"),
            ("effective_date", "31/03/2025"),
            ("effective_date", "2025-02-30"),
            ("effective_date", "2999-01-01"),
            ("version", "x" * 101),
            ("authority", "line\nbreak"),
        ],
    )
    def test_bad_values_are_rejected(self, field, value):
        with pytest.raises(ProvenanceError):
            build_provenance(**{field: value})

    def test_fields_left_out_keep_their_current_value(self):
        base = build_provenance(source_type="approved_answer", version="2", effective_date="2025-01-01")
        changed = build_provenance(base=base, version="3")
        assert (changed.source_type, changed.version, changed.effective_date) == ("approved_answer", "3", "2025-01-01")

    def test_the_index_fields_hold_a_real_date_not_the_ingest_time(self):
        fields = build_provenance(effective_date="2025-03-01").to_index_fields()
        assert fields["effective_date"] == fields["last_reviewed_date"] == "2025-03-01T00:00:00Z"
        assert build_provenance().to_index_fields()["effective_date"] is None

    def test_a_source_is_described_in_one_line(self):
        doc = {"source_type": "approved_answer", "approval_status": "approved", "effective_date": "2026-01-15",
               "version": "3", "section": "2. Security", "page": 4}
        assert describe_source(doc) == 'approved answer, approved, effective 2026-01-15, version 3, section "2. Security", page 4'
        assert describe_source({}) == ""


# ── Ranking ──────────────────────────────────────────────


class TestRankingPolicy:
    def weight(self, source_type="historical", status="unreviewed", effective="2026-10-22"):
        return POLICY.weight(source_type, status, effective, TODAY)

    def test_authoritative_outweighs_historical(self):
        assert self.weight("authoritative") > self.weight("approved_answer") > self.weight("historical") > self.weight("external")

    def test_old_documents_weigh_less_but_never_below_the_floor(self):
        fresh, old, ancient = self.weight(effective="2026-10-22"), self.weight(effective="2024-10-22"), self.weight(effective="1990-01-01")
        assert fresh == 1.0
        assert 0.7 < old < fresh
        assert ancient == pytest.approx(0.7, abs=0.001)

    def test_superseded_and_draft_material_is_demoted(self):
        assert self.weight(status="superseded") < self.weight(status="draft") < self.weight(status="approved")

    def test_an_undated_chunk_is_neither_fresh_nor_stale(self):
        assert self.weight(effective="") == pytest.approx(0.85)

    def test_freshness_can_be_switched_off(self):
        flat = RankingPolicy({}, {}, half_life_days=0, freshness_floor=0.7)
        assert flat.weight("historical", "unreviewed", "1990-01-01", TODAY) == 1.0

    def test_future_dated_chunks_count_as_new(self):
        assert self.weight(effective="2030-01-01") == 1.0


class TestSearchRanking:
    CURRENT = sourced(
        "security-policy-2026.pdf", "https://kb/a/p/g/security-policy-2026.pdf", score=0.030,
        content="Customer data is encrypted with AES-256 and keys rotate every 90 days.",
        source_type="authoritative", effective_date="2026-09-01T00:00:00Z", version="4", approval_status="approved",
        section="3. Encryption", page=7,
    )
    OLD = sourced(
        "proposal-2022.pdf", "https://kb/a/p/g/proposal-2022.pdf", score=0.033,  # the better text match
        content="Customer data is encrypted with AES-128 and keys rotate yearly.",
        source_type="historical", effective_date="2022-03-01T00:00:00Z", approval_status="unreviewed",
    )

    def test_the_current_authoritative_document_outranks_an_older_conflicting_answer(self, search):  # noqa: F811
        search(FakeSearchClient([self.OLD, self.CURRENT]))

        docs = kb_search.search_kb(query="how is data encrypted", **ACCESS)

        assert [d["title"] for d in docs] == ["security-policy-2026.pdf", "proposal-2022.pdf"]
        assert docs[0]["source_type"] == "authoritative"
        assert docs[0]["effective_date"] == "2026-09-01"
        assert docs[0]["version"] == "4"
        assert (docs[0]["section"], docs[0]["page"]) == ("3. Encryption", 7)
        assert docs[1]["source_type"] == "historical"  # the older answer is still returned, just later

    def test_ranking_can_be_neutralised_by_configuration(self, search):  # noqa: F811
        search(
            FakeSearchClient([self.OLD, self.CURRENT]),
            kb_tier_weights="authoritative=1,historical=1",
            kb_freshness_half_life_days=0,
        )
        docs = kb_search.search_kb(query="q", **ACCESS)
        assert [d["title"] for d in docs] == ["proposal-2022.pdf", "security-policy-2026.pdf"]

    def test_a_document_just_outside_the_top_k_can_be_promoted_into_it(self, search):  # noqa: F811
        filler = [sourced(f"old-{i}", f"https://kb/old-{i}.pdf", score=0.05 - i * 0.001, source_type="historical",
                          effective_date="2020-01-01T00:00:00Z") for i in range(5)]
        client = search(FakeSearchClient([*filler, self.CURRENT]), search_top_k=5)
        docs = kb_search.search_kb(query="q", **ACCESS)
        assert "security-policy-2026.pdf" in [d["title"] for d in docs]
        assert len(docs) == 5
        assert client.calls[0]["top"] == 15

    def test_chunks_from_before_provenance_still_work(self, search):  # noqa: F811
        search(FakeSearchClient([hit(score=0.5)]))
        (doc,) = kb_search.search_kb(query="q", **ACCESS)
        assert doc["source_type"] == "" and doc["page"] is None

    def test_an_index_without_the_new_fields_is_searched_without_them(self, search):  # noqa: F811
        error = HttpResponseError(message="Could not find a property named 'source_type' on type 'search.document'.")
        client = search(FakeSearchClient([hit()], errors=[error]))

        docs = kb_search.search_kb(query="q", **ACCESS)

        assert len(docs) == 1
        assert "source_type" in client.calls[0]["select"]
        assert client.calls[1]["select"] == ["content", "title", "source_url"]

    def test_provenance_text_is_redacted_like_content(self, search):  # noqa: F811
        search(FakeSearchClient([sourced("g", "https://kb/g.pdf", 1.0, section="Pricing: $9,999", version="v1")]))
        (doc,) = kb_search.search_kb(query="q", **ACCESS)
        assert "9,999" not in doc["section"]

    def test_the_prompt_labels_each_source(self):
        docs = [{**self.CURRENT, "effective_date": "2026-09-01", "content": "text"}]
        block = kb_search.format_context(docs)
        assert 'Source: authoritative, approved, effective 2026-09-01, version 4, section "3. Encryption", page 7' in block


# ── Evidence objects ─────────────────────────────────────


class TestEvidence:
    DOCS = [
        {"source_url": "https://kb/a.pdf", "content": "Intro text about the company and its history.", "source_type": "historical",
         "effective_date": "2022-03-01", "page": 1, "section": "1. Intro"},
        {"source_url": "https://kb/a.pdf", "content": "Data is encrypted with AES-256 at rest and in transit.",
         "source_type": "authoritative", "effective_date": "2026-09-01", "version": "4", "page": 7, "section": "3. Encryption"},
    ]

    def test_evidence_carries_the_provenance_of_the_chunk_that_holds_its_quote(self):
        answer = {"evidence": [{"title": "A", "source_url": "https://kb/a.pdf", "quote": "encrypted with AES-256 at rest"}]}
        (entry,) = kb_search.enrich_evidence(answer, self.DOCS)["evidence"]
        assert (entry["source_type"], entry["effective_date"], entry["version"]) == ("authoritative", "2026-09-01", "4")
        assert (entry["section"], entry["page"]) == ("3. Encryption", 7)

    def test_without_a_matching_quote_the_best_ranked_chunk_of_the_document_is_used(self):
        (entry,) = kb_search.enrich_evidence({"evidence": [{"source_url": "https://kb/a.pdf/", "quote": ""}]}, self.DOCS)["evidence"]
        assert entry["page"] == 1

    def test_unknown_sources_are_left_alone_and_the_original_is_not_modified(self):
        answer = {"evidence": [{"source_url": "https://other/x.pdf", "quote": "q"}]}
        assert kb_search.enrich_evidence(answer, self.DOCS) == answer
        original = {"evidence": [{"source_url": "https://kb/a.pdf", "quote": ""}]}
        kb_search.enrich_evidence(original, self.DOCS)
        assert "page" not in original["evidence"][0]

    def test_a_drafted_answer_returns_evidence_with_its_type_date_version_and_page(self, agent):  # noqa: F811
        agent.reply = lambda _p: json.dumps({
            "requirement_id": "R1", "answer_markdown": "Encrypted [A](https://kb/a.pdf).", "meets": "Yes",
            "evidence": [{"title": "A", "source_url": "https://kb/a.pdf", "quote": "Data is encrypted with AES-256 at rest"}],
        })
        result = answers.answer_single_requirement(
            requirement={"id": "R1", "text": "Encrypt data"}, context=RetrievedContext(self.DOCS), tone="professional",
            agent_id="agent-1",
        )
        (entry,) = result["evidence"]
        assert (entry["source_type"], entry["effective_date"], entry["version"], entry["page"]) == (
            "authoritative", "2026-09-01", "4", 7,
        )
        assert "Source: authoritative" in agent.prompts[0]

    def test_the_system_prompt_tells_the_model_how_to_treat_conflicting_sources(self):
        from app.agents.prompts import AGENT_INSTRUCTIONS

        assert "authoritative" in AGENT_INSTRUCTIONS and "older proposal" in AGENT_INSTRUCTIONS


# ── Chunks and the index ─────────────────────────────────


@pytest.fixture
def words(monkeypatch):
    monkeypatch.setattr(kb_ingest, "_tokenizer", lambda: WordTokenizer())


class TestChunks:
    TEXT = "Welcome to the guide.\n\n1. Security\nWe encrypt data.\n\n2. Support\nWe answer calls."

    def test_each_chunk_knows_its_section(self, words):
        chunks = kb_ingest.chunk_document(self.TEXT)
        assert [(c.section, c.text.splitlines()[0]) for c in chunks] == [
            ("", "Welcome to the guide."), ("1. Security", "1. Security"), ("2. Support", "2. Support"),
        ]

    def test_text_before_the_first_heading_is_kept(self, words):
        assert "Welcome to the guide." in [c.text.splitlines()[0] for c in kb_ingest.chunk_document(self.TEXT)]

    def test_a_long_section_keeps_its_heading_on_every_piece(self, words):
        text = "1. Alpha\n" + " ".join(f"a{i}" for i in range(30)) + "\n\n2. Beta\nshort"
        chunks = kb_ingest.chunk_document(text, max_tokens=10)
        assert [c.section for c in chunks].count("1. Alpha") > 1
        assert chunks[-1].section == "2. Beta"

    def test_pages_follow_the_offsets(self, words):
        chunks = kb_ingest.chunk_document(self.TEXT, page_starts=[(0, 1), (self.TEXT.index("2. Support"), 2)])
        assert [c.page for c in chunks] == [1, 1, 2]

    def test_text_without_headings_is_still_split(self, words):
        chunks = kb_ingest.chunk_document(" ".join(f"w{i}" for i in range(25)), max_tokens=10)
        assert len(chunks) > 2 and all(c.section == "" for c in chunks)
        assert chunks[-1].text.endswith("w24")


class TestIndexSchema:
    names = {f.name: f for f in kb_ingest.get_index_schema("kb").fields}

    def test_provenance_and_location_fields_exist(self):
        assert {"source_type", "authority", "effective_date", "version", "owner", "approval_status", "section", "page"} <= set(self.names)

    def test_the_old_review_field_stays_because_an_index_field_cannot_be_dropped_in_place(self):
        assert "last_reviewed_date" in self.names

    def test_tiers_and_dates_can_be_filtered(self):
        assert all(self.names[n].filterable for n in ("source_type", "effective_date", "approval_status", "page"))


class TestIngestRecordsProvenance:
    @pytest.fixture
    def pipeline(self, monkeypatch, words):
        state = SimpleNamespace(uploaded=[], data=b"")
        monkeypatch.setattr(kb_ingest, "_ensure_index_once", lambda: None)
        monkeypatch.setattr(
            azure_clients, "container",
            lambda _s, _n: SimpleNamespace(download_blob=lambda _b: SimpleNamespace(readall=lambda: state.data)),
        )
        monkeypatch.setattr(kb_ingest, "_embed_texts", lambda texts: [[0.1]] * len(texts))
        monkeypatch.setattr(azure_clients, "search_client", lambda _s: SimpleNamespace(upload_documents=state.uploaded.extend))
        monkeypatch.setattr(kb_ingest, "get_settings", lambda: settings())
        return state

    def test_every_chunk_carries_provenance_section_and_page(self, pipeline):
        pipeline.data = make_pdf([
            ["1. Overview", "We are a cloud company."],
            ["2. Security", "All data is encrypted at rest."],
        ])
        provenance = build_provenance(source_type="authoritative", effective_date="2026-09-01", version="4", approval_status="approved")

        count = kb_ingest.ingest_blob("a/p/g/policy.pdf", "a", "p", "g", oids=["u"], provenance=provenance)

        assert count == len(pipeline.uploaded) == 2
        first, second = pipeline.uploaded
        assert (first["section"], first["page"]) == ("1. Overview", 1)
        assert (second["section"], second["page"]) == ("2. Security", 2)
        for chunk in pipeline.uploaded:
            assert chunk["source_type"] == "authoritative" and chunk["version"] == "4"
            assert chunk["effective_date"] == chunk["last_reviewed_date"] == "2026-09-01T00:00:00Z"
            assert chunk["approval_status"] == "approved"

    def test_without_provenance_a_document_is_historical(self, pipeline):
        pipeline.data = make_pdf([["1. Overview", "Text of the guide goes here."]])
        kb_ingest.ingest_blob("a/p/g/policy.pdf", "a", "p", "g")
        assert pipeline.uploaded[0]["source_type"] == "historical"
        assert pipeline.uploaded[0]["effective_date"] is None

    def test_redaction_keeps_page_positions_right(self, pipeline, monkeypatch):
        engine = get_redaction_engine()
        pipeline.data = make_pdf([
            ["1. One", "Mail someone@example.com about the price of $50,000 and more words to lengthen this."],
            ["2. Two", "Second page text."],
        ])
        kb_ingest.ingest_blob("a/p/g/policy.pdf", "a", "p", "g")
        assert [(c["section"], c["page"]) for c in pipeline.uploaded] == [("1. One", 1), ("2. Two", 2)]
        assert "someone@example.com" not in pipeline.uploaded[0]["content"]
        assert engine is not None


# ── The KB API ───────────────────────────────────────────


class TestKbApi:
    async def test_provenance_is_recorded_on_the_blob_and_indexed(self, client, world):
        response = await upload_kb(client, A, source_type="approved_answer", version="2", authority="Bid team",
                                   effective_date="2025-06-01", approval_status="approved")

        assert response.status_code == 200, response.text
        assert response.json()["provenance"]["source_type"] == "approved_answer"
        recorded = provenance_from_metadata(world.storage.kb.blobs[KB_BLOB].metadata)
        assert (recorded.version, recorded.effective_date, recorded.authority) == ("2", "2025-06-01", "Bid team")
        assert world.ingests[0]["provenance"] == recorded

    async def test_only_an_administrator_can_mark_a_source_authoritative(self, client, world):
        denied = await upload_kb(client, A, source_type="authoritative")
        assert denied.status_code == 403 and KB_BLOB not in world.storage.kb.blobs

        allowed = await upload_kb(client, ADMIN, source_type="authoritative")
        assert allowed.status_code == 200 and allowed.json()["provenance"]["source_type"] == "authoritative"

    @pytest.mark.parametrize("form", [{"effective_date": "tomorrow"}, {"source_type": "gold"}, {"approval_status": "x"}])
    async def test_invalid_values_are_a_400(self, client, form):
        assert (await upload_kb(client, A, **form)).status_code == 400

    async def test_a_new_upload_is_effective_from_today_unless_it_says_otherwise(self, client):
        body = (await upload_kb(client, A)).json()
        assert body["provenance"]["effective_date"] == datetime.now(UTC).date().isoformat()

    async def test_replacing_a_document_keeps_the_provenance_it_had(self, client):
        await upload_kb(client, A, source_type="approved_answer", version="2", authority="Bid team")
        again = (await upload_kb(client, A)).json()["provenance"]
        assert (again["source_type"], again["version"], again["authority"]) == ("approved_answer", "2", "Bid team")

    async def test_blank_type_and_status_mean_not_given(self, client):
        await upload_kb(client, A, source_type="approved_answer", approval_status="approved")
        again = (await upload_kb(client, A, source_type="", approval_status="")).json()["provenance"]
        assert (again["source_type"], again["approval_status"]) == ("approved_answer", "approved")

    async def test_an_owner_cannot_replace_the_content_of_an_authoritative_document(self, client, world):
        await upload_kb(client, ADMIN, source_type="authoritative", data=b"%PDF-1.4 policy v1")
        # an admin hands the document to a user by making the user its owner
        world.storage.kb.blobs[KB_BLOB].metadata["owner_oid"] = "user-a"

        replaced = await upload_kb(client, A, data=b"%PDF-1.4 tampered")

        assert replaced.status_code == 403
        assert world.storage.kb.blobs[KB_BLOB].data == b"%PDF-1.4 policy v1"

    async def test_an_owner_can_still_reingest_or_demote_an_authoritative_document(self, client, world):
        await upload_kb(client, ADMIN, source_type="authoritative")
        world.storage.kb.blobs[KB_BLOB].metadata["owner_oid"] = "user-a"

        same = await client.post("/kb/ingest", json={"blob_name": KB_BLOB, "redaction_level": "strict"}, headers=A)
        assert same.status_code == 200 and same.json()["provenance"]["source_type"] == "authoritative"

        demoted = await client.post("/kb/ingest", json={"blob_name": KB_BLOB, "source_type": "historical"}, headers=A)
        assert demoted.status_code == 200 and demoted.json()["provenance"]["source_type"] == "historical"

    async def test_an_admin_can_replace_an_authoritative_document(self, client):
        await upload_kb(client, ADMIN, source_type="authoritative", version="1")
        again = await upload_kb(client, ADMIN, version="2")
        assert again.status_code == 200
        assert again.json()["provenance"]["source_type"] == "authoritative"

    async def test_the_list_shows_provenance(self, client):
        await upload_kb(client, A, source_type="external", version="9")
        (doc,) = (await client.get("/kb/list", headers=A)).json()["documents"]
        assert doc["provenance"]["source_type"] == "external" and doc["provenance"]["version"] == "9"

    async def test_legacy_documents_list_with_safe_defaults(self, client, world):
        world.storage.kb.upload_blob(KB_BLOB, b"x", metadata={"owner_oid": "user-a", "shared_groups": ""})
        (doc,) = (await client.get("/kb/list", headers=A)).json()["documents"]
        assert doc["provenance"]["source_type"] == "historical"
        assert doc["provenance"]["effective_date"] == "2025-01-01"  # the file's own date

    async def test_reingest_can_change_provenance_without_a_new_upload(self, client, world):
        await upload_kb(client, A)
        world.ingests.clear()

        response = await client.post("/kb/ingest", json={"blob_name": KB_BLOB, "source_type": "approved_answer", "version": "5"}, headers=A)

        assert response.status_code == 200, response.text
        assert world.ingests[0]["provenance"].source_type == "approved_answer"
        assert provenance_from_metadata(world.storage.kb.blobs[KB_BLOB].metadata).version == "5"
        assert world.storage.kb.blobs[KB_BLOB].metadata["owner_oid"] == "user-a"  # access rights are untouched

    async def test_reingest_cannot_promote_to_authoritative_without_being_an_admin(self, client, world):
        await upload_kb(client, A)
        response = await client.post("/kb/ingest", json={"blob_name": KB_BLOB, "source_type": "authoritative"}, headers=A)
        assert response.status_code == 403
        assert provenance_from_metadata(world.storage.kb.blobs[KB_BLOB].metadata).source_type == "historical"

    async def test_reindex_keeps_every_documents_provenance(self, client, world):
        await upload_kb(client, A, source_type="approved_answer", version="2", effective_date="2025-06-01")
        world.ingests.clear()

        response = await client.post("/kb/reindex", headers=ADMIN)

        assert response.status_code == 200
        (call,) = world.ingests
        assert (call["provenance"].source_type, call["provenance"].version) == ("approved_answer", "2")

    async def test_reindex_defaults_documents_that_never_had_provenance(self, client, world):
        world.storage.kb.upload_blob(KB_BLOB, b"x", metadata={"owner_oid": "user-a", "shared_groups": ""})
        await client.post("/kb/reindex", headers=ADMIN)
        assert world.ingests[0]["provenance"] == Provenance(effective_date="2025-01-01")
