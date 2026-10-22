"""Evidence links: opaque references the app serves behind sign-in, instead of private blob URLs."""

import json

from app import kb_refs
from app.agents import answers, kb_search
from app.agents.kb_search import RetrievedContext

from .test_authorization_api import ADMIN, KB_BLOB, A, B, C, upload_kb
from .test_draft_endpoints import agent  # noqa: F401  (a fixture)
from .test_kb_search import ACCESS, FakeSearchClient, hit, search  # noqa: F401  (search is a fixture)

STORAGE = "https://example.blob.core.windows.net"
CONTAINER = "knowledge-base"
BLOB = "acme-corp/project-x/general/Encryption Guide (v2).pdf"
BLOB_URL = f"{STORAGE}/{CONTAINER}/{BLOB}"


class TestReferences:
    def test_a_knowledge_base_url_becomes_a_hash_that_reveals_nothing(self):
        link = kb_refs.opaque_url(BLOB_URL, STORAGE, CONTAINER)
        assert link == f"/kb/doc/{kb_refs.doc_ref(BLOB)}"
        assert kb_refs.is_ref(link.rsplit("/", 1)[-1])
        assert "acme" not in link and "pdf" not in link

    def test_other_urls_are_left_alone(self):
        for url in ("https://learn.microsoft.com/azure", f"{STORAGE}/other-container/x.pdf", f"{STORAGE}/{CONTAINER}/", ""):
            assert kb_refs.opaque_url(url, STORAGE, CONTAINER) == url

    def test_the_blob_name_is_recovered_from_its_url(self):
        assert kb_refs.blob_name_of_url(BLOB_URL, STORAGE, CONTAINER) == BLOB
        assert kb_refs.blob_name_of_url("https://elsewhere/x", STORAGE, CONTAINER) is None

    def test_the_same_blob_always_has_the_same_reference(self):
        assert kb_refs.doc_ref(BLOB) == kb_refs.doc_ref(BLOB) != kb_refs.doc_ref(BLOB + "x")

    def test_the_index_lists_blobs_only_for_references_it_has_not_seen(self):
        calls = []

        def names():
            calls.append(1)
            return [BLOB, "a/b/c/d.pdf"]

        index = kb_refs.RefIndex()
        assert index.resolve(kb_refs.doc_ref(BLOB), names) == BLOB
        assert index.resolve(kb_refs.doc_ref(BLOB), names) == BLOB
        assert len(calls) == 1
        assert index.resolve("0" * 24, names) is None  # unknown: listed again
        assert len(calls) == 2


class TestRetrievalUsesReferences:
    def retrieve(self, search):  # noqa: F811
        search(FakeSearchClient([hit(title="Encryption Guide", url=BLOB_URL, content="Data is encrypted with AES-256.")]))
        return kb_search.search_kb(query="encryption", **ACCESS)

    def test_a_retrieved_source_carries_the_reference_not_the_blob_url(self, search):  # noqa: F811
        (doc,) = self.retrieve(search)
        assert doc["source_url"] == f"/kb/doc/{kb_refs.doc_ref(BLOB)}"
        assert doc["blob_name"] == BLOB  # kept for the server (evaluation), never put in a prompt

    def test_the_prompt_never_shows_the_customer_folder_or_the_storage_account(self, search, agent):  # noqa: F811
        context = RetrievedContext(self.retrieve(search))
        agent.reply = lambda _p: json.dumps({"requirement_id": "R1", "answer_markdown": "ok", "meets": "Yes"})

        answers.answer_single_requirement(
            requirement={"id": "R1", "text": "Encrypt data"}, context=context, tone="professional", agent_id="agent-1"
        )

        prompt = agent.prompts[0]
        assert "/kb/doc/" in prompt
        for leak in ("acme-corp", "project-x", "example.blob.core.windows.net", "knowledge-base/"):
            assert leak not in prompt

    def test_the_model_may_cite_the_reference_but_not_the_raw_blob_url(self, search, agent):  # noqa: F811
        context = RetrievedContext(self.retrieve(search))
        ref = f"/kb/doc/{kb_refs.doc_ref(BLOB)}"
        agent.reply = lambda _p: json.dumps(
            {
                "requirement_id": "R1",
                "answer_markdown": f"Encrypted [guide]({ref}); also [raw]({BLOB_URL}) and [fake](/kb/doc/{'a' * 24}).",
                "meets": "Yes",
                "evidence": [{"title": "Guide", "source_url": ref, "quote": "Data is encrypted with AES-256."}],
            }
        )
        result = answers.answer_single_requirement(
            requirement={"id": "R1", "text": "Encrypt data"}, context=context, tone="professional", agent_id="agent-1"
        )
        assert f"[guide]({ref})" in result["answer_markdown"]
        assert BLOB_URL not in result["answer_markdown"] and "a" * 24 not in result["answer_markdown"]
        (entry,) = result["evidence"]
        assert entry["source_url"] == ref and "page" not in entry

    def test_a_cut_off_reference_is_not_accepted_as_a_citation(self):
        allowed = {f"/kb/doc/{kb_refs.doc_ref(BLOB)}"}
        text = "[a](/kb/doc/abc) [b](/kb/doc/)"
        cleaned = kb_search.sanitize_citations(text, allowed)
        assert "](" not in cleaned

    def test_web_urls_still_follow_the_old_prefix_rule_for_parenthesised_names(self):
        allowed = {"https://kb.example/files/report (final).pdf"}
        assert "](" in kb_search.sanitize_citations("[r](https://kb.example/files/report (final)", allowed)


class TestOpeningADocument:
    def link(self, blob_name=KB_BLOB):
        return f"/kb/doc/{kb_refs.doc_ref(blob_name)}"

    async def test_the_owner_opens_a_pdf_inline(self, client, world):
        await upload_kb(client, A, data=b"%PDF-1.4 owner copy")

        response = await client.get(self.link(), headers=A)

        assert response.status_code == 200
        assert response.content == b"%PDF-1.4 owner copy"
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"].startswith("inline;")
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"

    async def test_a_word_document_downloads(self, client):
        await upload_kb(client, A, name="guide.docx", data=b"PK word")
        response = await client.get(self.link("acme/p1/general/guide.docx"), headers=A)
        assert response.status_code == 200
        assert response.headers["content-disposition"].startswith("attachment;")
        assert response.headers["content-type"].endswith("wordprocessingml.document")

    async def test_a_group_member_opens_a_shared_document(self, client):
        await upload_kb(client, A, share="team-1")
        assert (await client.get(self.link(), headers=B)).status_code == 200

    async def test_a_stranger_gets_a_404_not_a_403(self, client):
        await upload_kb(client, A)
        assert (await client.get(self.link(), headers=B)).status_code == 404  # same group, but it is not shared
        assert (await client.get(self.link(), headers=C)).status_code == 404

    async def test_a_document_shared_with_another_group_is_not_open_to_everyone(self, client):
        await upload_kb(client, A, share="team-1")
        assert (await client.get(self.link(), headers=C)).status_code == 404

    async def test_an_administrator_can_open_any_document(self, client):
        await upload_kb(client, A)
        assert (await client.get(self.link(), headers=ADMIN)).status_code == 200

    async def test_an_unknown_reference_is_a_404_and_a_malformed_one_is_rejected(self, client):
        assert (await client.get(f"/kb/doc/{'0' * 24}", headers=A)).status_code == 404
        for bad in ("short", "G" * 24, "0" * 25, "..%2f..%2fsecret"):
            assert (await client.get(f"/kb/doc/{bad}", headers=A)).status_code in (404, 422)

    async def test_the_response_does_not_reveal_whether_a_hidden_document_exists(self, client):
        await upload_kb(client, A)
        hidden = await client.get(self.link(), headers=C)
        missing = await client.get(f"/kb/doc/{'1' * 24}", headers=C)
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json()

    async def test_a_deleted_document_is_gone(self, client):
        await upload_kb(client, A)
        assert (await client.get(self.link(), headers=A)).status_code == 200
        await client.delete("/kb/delete", params={"blob_name": KB_BLOB}, headers=A)
        assert (await client.get(self.link(), headers=A)).status_code == 404

    async def test_a_document_uploaded_after_the_references_were_first_listed_can_be_opened(self, client):
        await upload_kb(client, A, name="first.pdf")
        await client.get(self.link("acme/p1/general/first.pdf"), headers=A)
        await upload_kb(client, A, name="second.pdf")
        assert (await client.get(self.link("acme/p1/general/second.pdf"), headers=A)).status_code == 200

    async def test_a_signed_out_request_is_refused(self, client, use_settings):
        use_settings("jwt", entra_tenant_id="tenant", entra_client_id="client")
        assert (await client.get(self.link())).status_code in (401, 403)

    async def test_a_file_name_with_unusual_characters_downloads_safely(self, client):
        assert (await upload_kb(client, A, name="Résumé final (v2).pdf")).status_code == 200
        response = await client.get(self.link("acme/p1/general/Résumé final (v2).pdf"), headers=A)
        assert response.status_code == 200
        disposition = response.headers["content-disposition"]
        assert "filename*=UTF-8''R%C3%A9sum%C3%A9%20final%20%28v2%29.pdf" in disposition


def test_the_evaluation_matches_a_document_by_its_blob_name_too():
    from app.evaluation import _matches

    doc = {"title": "g.pdf", "source_url": "/kb/doc/abc", "blob_name": "acme/p1/general/g.pdf"}
    assert _matches(doc, "acme/p1")
    assert not _matches(doc, "other")
