"""API-level authorisation tests: ownership of RFPs and KB documents, admin-only operations,
input validation, upload limits and rate limiting. Azure Storage/Search are replaced by in-memory fakes."""

import base64
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from azure.core.exceptions import ResourceNotFoundError

from app import main
from app.auth.ratelimit import limiter
from app.config import Settings, get_settings

SAMPLE_DOCX = (Path(__file__).parent / "fixtures" / "sample_rfp.docx").read_bytes()
KB_BLOB = "acme/p1/general/guide.pdf"


# ── In-memory Azure fakes ────────────────────────────────


class FakeBlob:
    def __init__(self, name, data, metadata):
        self.name = name
        self.data = data
        self.metadata = dict(metadata) if metadata else {}
        self.size = len(data)
        self.last_modified = datetime(2025, 1, 1, tzinfo=UTC)


class FakeBlobClient:
    def __init__(self, container, name):
        self._container = container
        self._name = name

    def get_blob_properties(self):
        blob = self._container.blobs.get(self._name)
        if blob is None:
            raise ResourceNotFoundError("missing")
        return blob

    def set_blob_metadata(self, metadata):
        self._container.blobs[self._name].metadata = dict(metadata)


class FakeContainer:
    def __init__(self):
        self.blobs: dict[str, FakeBlob] = {}

    def list_blobs(self, name_starts_with=None, include=None):
        return [b for _, b in sorted(self.blobs.items()) if name_starts_with is None or b.name.startswith(name_starts_with)]

    def get_blob_client(self, name):
        return FakeBlobClient(self, name)

    def upload_blob(self, name, data, overwrite=False, metadata=None):
        self.blobs[name] = FakeBlob(name, bytes(data), metadata)

    def download_blob(self, name):
        blob = self.blobs.get(name)
        if blob is None:
            raise ResourceNotFoundError("missing")
        return SimpleNamespace(readall=lambda: blob.data)

    def delete_blob(self, name):
        del self.blobs[name]

    def create_container(self):
        pass


class FakeStorage:
    """Stands in for the BlobServiceClient class."""

    def __init__(self):
        self.containers = defaultdict(FakeContainer)

    def __call__(self, account_url=None, credential=None):
        return SimpleNamespace(get_container_client=lambda name: self.containers[name])

    @property
    def rfps(self):
        return self.containers["rfp-uploads"]

    @property
    def kb(self):
        return self.containers["knowledge-base"]

    @property
    def outputs(self):
        return self.containers["outputs"]


class FakeSearch:
    """Stands in for the SearchClient class; records delete filters and deletions."""

    def __init__(self):
        self.filters: list[str] = []
        self.deleted: list[dict] = []

    def __call__(self, endpoint=None, index_name=None, credential=None):
        outer = self

        class Client:
            def search(self, **kwargs):
                outer.filters.append(kwargs["filter"])
                return [{"id": "chunk-1"}, {"id": "chunk-2"}]

            def delete_documents(self, documents):
                outer.deleted.extend(documents)

        return Client()


@pytest.fixture
def world(monkeypatch):
    limiter.reset()
    storage, search, ingests = FakeStorage(), FakeSearch(), []

    def fake_ingest(**kwargs):
        ingests.append(kwargs)
        return 3

    monkeypatch.setattr("azure.storage.blob.BlobServiceClient", storage)
    monkeypatch.setattr("azure.search.documents.SearchClient", search)
    monkeypatch.setattr("app.ingestion.kb_ingest.ingest_blob", fake_ingest)
    return SimpleNamespace(storage=storage, search=search, ingests=ingests)


def make_settings(mode="easyauth", admin_oids="admin-1", **overrides) -> Settings:
    return Settings(
        _env_file=None,
        auth_mode=mode,
        admin_oids=admin_oids,
        foundry_project_endpoint="https://example.services.ai.azure.com",
        azure_ai_search_connection_name="search",
        azure_storage_account_url="https://example.blob.core.windows.net",
        **overrides,
    )


@pytest.fixture
def use_settings():
    def _use(mode="easyauth", **overrides):
        settings = make_settings(mode, **overrides)
        main.app.dependency_overrides[get_settings] = lambda: settings
        return settings

    _use()
    yield _use
    main.app.dependency_overrides.clear()


@pytest.fixture
async def client(world, use_settings):
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def as_user(oid, *groups):
    claims = [{"typ": "oid", "val": oid}] + [{"typ": "groups", "val": g} for g in groups]
    principal = base64.b64encode(json.dumps({"auth_typ": "aad", "claims": claims}).encode()).decode()
    return {"x-ms-client-principal": principal, "x-ms-client-principal-id": oid}


A = as_user("user-a", "team-1")
B = as_user("user-b", "team-1")  # same group as A
C = as_user("user-c", "team-9")  # unrelated user
ADMIN = as_user("admin-1")


async def upload_rfp(client, who, data=SAMPLE_DOCX, name="rfp.docx"):
    response = await client.post("/rfp/upload", files={"file": (name, data)}, headers=who)
    assert response.status_code == 200, response.text
    return response.json()["rfp_id"]


async def upload_kb(client, who, name="guide.pdf", share="", data=b"%PDF-1.4 sample", **form):
    fields = {"client_id": "acme", "project_id": "p1", "solution_area": "general", "share_with_groups": share, **form}
    return await client.post("/kb/upload", files={"file": (name, data)}, data=fields, headers=who)


# ── RFP ownership ────────────────────────────────────────


RFP_ENDPOINTS = [
    ("/rfp/extract", lambda rid: {"rfp_id": rid, "filename": "rfp.docx"}),
    ("/rfp/extract-ai", lambda rid: {"rfp_id": rid, "filename": "rfp.docx"}),
    ("/draft", lambda rid: {"rfp_id": rid, "client_id": "acme", "project_id": "p1"}),
    ("/draft/stream", lambda rid: {"rfp_id": rid, "client_id": "acme", "project_id": "p1"}),
    ("/draft/generate-doc", lambda rid: {"rfp_id": rid, "responses": [{"requirement_id": "R1"}]}),
]


class TestRfpOwnership:
    async def test_upload_records_the_uploader_as_owner(self, client, world):
        rfp_id = await upload_rfp(client, A)
        (blob,) = world.storage.rfps.list_blobs(name_starts_with=f"{rfp_id}/")
        assert blob.metadata == {"owner_oid": "user-a"}

    async def test_the_owner_and_an_admin_can_extract(self, client):
        rfp_id = await upload_rfp(client, A)
        body = {"rfp_id": rfp_id, "filename": "rfp.docx"}
        assert (await client.post("/rfp/extract", json=body, headers=A)).status_code == 200
        assert (await client.post("/rfp/extract", json=body, headers=ADMIN)).status_code == 200

    @pytest.mark.parametrize(("path", "build"), RFP_ENDPOINTS)
    async def test_other_users_get_404(self, client, path, build):
        rfp_id = await upload_rfp(client, A)
        for outsider in (B, C):
            response = await client.post(path, json=build(rfp_id), headers=outsider)
            assert response.status_code == 404, f"{path} leaked to another user"

    async def test_an_rfp_without_a_recorded_owner_is_admin_only(self, client, world):
        world.storage.rfps.upload_blob("legacy1/rfp.docx", SAMPLE_DOCX)  # uploaded before ownership existed
        body = {"rfp_id": "legacy1", "filename": "rfp.docx"}
        assert (await client.post("/rfp/extract", json=body, headers=A)).status_code == 404
        assert (await client.post("/rfp/extract", json=body, headers=ADMIN)).status_code == 200

    async def test_outputs_follow_rfp_ownership(self, client, world, monkeypatch):
        monkeypatch.setattr(
            "app.agents.foundry_agent.run_draft",
            lambda **kwargs: {"executive_summary": "x", "solution_overview": "y", "responses": []},
        )
        rfp_id = await upload_rfp(client, A)
        draft = await client.post(
            "/draft", json={"rfp_id": rfp_id, "client_id": "acme", "project_id": "p1"}, headers=A
        )
        assert draft.status_code == 200, draft.text

        owner = await client.get(f"/outputs/{rfp_id}/draft.docx", headers=A)
        assert owner.status_code == 200
        assert owner.headers["content-type"].startswith("application/vnd.openxmlformats")
        assert (await client.get(f"/outputs/{rfp_id}/draft.docx", headers=ADMIN)).status_code == 200
        assert (await client.get(f"/outputs/{rfp_id}/draft.docx", headers=B)).status_code == 404
        assert (await client.get(f"/outputs/{rfp_id}/compliance.xlsx", headers=C)).status_code == 404

    async def test_only_the_generated_files_can_be_downloaded(self, client, world):
        rfp_id = await upload_rfp(client, A)
        world.storage.outputs.upload_blob(f"{rfp_id}/notes.txt", b"secret")
        response = await client.get(f"/outputs/{rfp_id}/notes.txt", headers=A)
        assert response.status_code == 404

    async def test_malformed_ids_are_rejected(self, client):
        assert (await client.get("/outputs/not%20valid/draft.docx", headers=A)).status_code == 422
        body = {"rfp_id": "x/../y", "filename": "rfp.docx"}
        assert (await client.post("/rfp/extract", json=body, headers=A)).status_code == 422

    async def test_with_authentication_disabled_everything_is_accessible(self, client, use_settings):
        use_settings("disabled")
        rfp_id = await upload_rfp(client, {})
        body = {"rfp_id": rfp_id, "filename": "rfp.docx"}
        assert (await client.post("/rfp/extract", json=body)).status_code == 200


# ── KB ownership ─────────────────────────────────────────


class TestKbUpload:
    async def test_documents_are_private_to_the_uploader_by_default(self, client, world):
        response = await upload_kb(client, A)

        assert response.status_code == 200, response.text
        assert world.storage.kb.blobs[KB_BLOB].metadata == {"owner_oid": "user-a", "shared_groups": ""}
        (ingest,) = world.ingests
        assert ingest["oids"] == ["user-a"]
        assert ingest["groups"] == []
        assert (ingest["client_id"], ingest["project_id"], ingest["solution_area"]) == ("acme", "p1", "general")

    async def test_a_document_can_be_shared_with_the_uploaders_own_groups(self, client, world):
        response = await upload_kb(client, A, share="team-1")

        assert response.status_code == 200
        assert response.json()["shared_with_groups"] == ["team-1"]
        assert world.ingests[0]["groups"] == ["team-1"]
        assert world.storage.kb.blobs[KB_BLOB].metadata["shared_groups"] == "team-1"

    async def test_sharing_with_a_group_you_are_not_in_is_refused(self, client, world):
        response = await upload_kb(client, A, share="team-1,finance")

        assert response.status_code == 403
        assert world.storage.kb.blobs == {}
        assert world.ingests == []

    async def test_admins_can_share_with_any_group(self, client, world):
        assert (await upload_kb(client, ADMIN, share="finance")).status_code == 200

    async def test_invalid_group_ids_are_rejected(self, client):
        assert (await upload_kb(client, ADMIN, share="a b")).status_code == 400

    async def test_another_user_cannot_overwrite_a_document(self, client, world):
        await upload_kb(client, A, data=b"%PDF original")
        world.ingests.clear()

        response = await upload_kb(client, B, data=b"%PDF replacement")

        assert response.status_code == 403
        assert world.storage.kb.blobs[KB_BLOB].data == b"%PDF original"
        assert world.storage.kb.blobs[KB_BLOB].metadata["owner_oid"] == "user-a"
        assert world.ingests == []

    async def test_the_owner_can_replace_a_document_and_change_its_sharing(self, client, world):
        await upload_kb(client, A)
        await upload_kb(client, A, share="team-1")
        assert world.storage.kb.blobs[KB_BLOB].metadata["shared_groups"] == "team-1"

    async def test_an_admin_replacing_a_document_keeps_its_owner(self, client, world):
        await upload_kb(client, A)
        world.ingests.clear()

        assert (await upload_kb(client, ADMIN)).status_code == 200

        assert world.storage.kb.blobs[KB_BLOB].metadata["owner_oid"] == "user-a"
        assert world.ingests[0]["oids"] == ["user-a"]


async def seed_kb(world):
    kb = world.storage.kb
    kb.upload_blob("acme/p1/general/private.pdf", b"x", metadata={"owner_oid": "user-a", "shared_groups": ""})
    kb.upload_blob("acme/p1/general/shared.pdf", b"x", metadata={"owner_oid": "user-a", "shared_groups": "team-1"})
    kb.upload_blob("acme/p1/general/legacy.pdf", b"x")  # no recorded owner


class TestKbList:
    async def names(self, client, who):
        response = await client.get("/kb/list", headers=who)
        assert response.status_code == 200
        return {d["filename"]: d for d in response.json()["documents"]}

    async def test_each_user_sees_only_what_they_may_use(self, client, world):
        await seed_kb(world)

        assert set(await self.names(client, A)) == {"private.pdf", "shared.pdf"}
        assert set(await self.names(client, B)) == {"shared.pdf"}  # via the shared group
        assert set(await self.names(client, C)) == set()
        assert set(await self.names(client, ADMIN)) == {"private.pdf", "shared.pdf", "legacy.pdf"}

    async def test_delete_is_offered_only_to_those_who_can_delete(self, client, world):
        await seed_kb(world)

        owner = (await self.names(client, A))["shared.pdf"]
        viewer = (await self.names(client, B))["shared.pdf"]

        assert owner["can_delete"] and owner["owned_by_me"]
        assert not viewer["can_delete"] and not viewer["owned_by_me"]
        assert owner["shared_with_groups"] == ["team-1"]


class TestKbDelete:
    async def test_an_unrelated_user_cannot_see_or_delete_it(self, client, world):
        await upload_kb(client, A)

        response = await client.delete("/kb/delete", params={"blob_name": KB_BLOB}, headers=C)

        assert response.status_code == 404
        assert KB_BLOB in world.storage.kb.blobs
        assert world.search.deleted == []

    async def test_someone_it_is_shared_with_can_see_it_but_not_delete_it(self, client, world):
        await upload_kb(client, A, share="team-1")

        response = await client.delete("/kb/delete", params={"blob_name": KB_BLOB}, headers=B)

        assert response.status_code == 403
        assert KB_BLOB in world.storage.kb.blobs
        assert world.search.deleted == []

    async def test_the_owner_deletes_the_blob_and_its_chunks(self, client, world):
        await upload_kb(client, A)

        response = await client.delete("/kb/delete", params={"blob_name": KB_BLOB}, headers=A)

        assert response.status_code == 200
        assert response.json()["deleted_chunks"] == 2
        assert KB_BLOB not in world.storage.kb.blobs
        assert world.search.deleted == [{"id": "chunk-1"}, {"id": "chunk-2"}]

    async def test_an_admin_can_delete_any_document(self, client, world):
        await upload_kb(client, A)
        response = await client.delete("/kb/delete", params={"blob_name": KB_BLOB}, headers=ADMIN)
        assert response.status_code == 200

    async def test_quotes_in_the_file_name_cannot_break_out_of_the_search_filter(self, client, world):
        name = "acme/p1/general/o'brien.pdf"
        await upload_kb(client, A, name="o'brien.pdf")

        await client.delete("/kb/delete", params={"blob_name": name}, headers=A)

        (odata_filter,) = world.search.filters
        assert odata_filter.endswith("o''brien.pdf'")  # quote doubled, so it stays inside the literal
        assert "o'brien" not in odata_filter

    async def test_unknown_or_malformed_names(self, client):
        assert (await client.delete("/kb/delete", params={"blob_name": "acme/p1/general/none.pdf"}, headers=A)).status_code == 404
        assert (await client.delete("/kb/delete", params={"blob_name": "../../etc/passwd"}, headers=A)).status_code == 422


class TestKbIngest:
    async def ingest(self, client, who, **body):
        return await client.post("/kb/ingest", json={"blob_name": KB_BLOB, **body}, headers=who)

    async def test_the_owner_reingests_with_the_recorded_access(self, client, world):
        await upload_kb(client, A, share="team-1")
        world.ingests.clear()

        response = await self.ingest(client, A)

        assert response.status_code == 200
        (call,) = world.ingests
        assert call["oids"] == ["user-a"]
        assert call["groups"] == ["team-1"]

    async def test_a_user_cannot_grant_themselves_access_to_someone_elses_document(self, client, world):
        await upload_kb(client, A)
        world.ingests.clear()

        assert (await self.ingest(client, C)).status_code == 404  # cannot even see it
        assert (await self.ingest(client, B)).status_code == 404  # private: not visible to a teammate
        assert world.ingests == []

    async def test_a_viewer_through_a_shared_group_cannot_reingest(self, client, world):
        await upload_kb(client, A, share="team-1")
        world.ingests.clear()

        assert (await self.ingest(client, B)).status_code == 403
        assert world.ingests == []

    async def test_callers_cannot_choose_who_can_read_a_document(self, client, world):
        await upload_kb(client, A)

        for field in ({"oids": ["user-c"]}, {"groups": ["team-9"]}):
            response = await self.ingest(client, A, **field)
            assert response.status_code == 422

    async def test_an_admin_claims_a_document_that_has_no_owner(self, client, world):
        world.storage.kb.upload_blob(KB_BLOB, b"x")

        assert (await self.ingest(client, A)).status_code == 404  # not visible to non-admins
        response = await self.ingest(client, ADMIN)

        assert response.status_code == 200
        assert world.storage.kb.blobs[KB_BLOB].metadata["owner_oid"] == "admin-1"
        assert world.ingests[-1]["oids"] == ["admin-1"]

    async def test_malformed_blob_names_are_rejected(self, client):
        response = await client.post("/kb/ingest", json={"blob_name": "../x/y/z.pdf"}, headers=A)
        assert response.status_code == 422


class TestKbReindex:
    async def test_only_admins_can_reindex(self, client, world):
        await seed_kb(world)
        assert (await client.post("/kb/reindex", headers=A)).status_code == 403
        assert world.ingests == []

    async def test_each_document_keeps_its_own_access_and_ownerless_ones_are_skipped(self, client, world):
        await seed_kb(world)
        world.storage.kb.upload_blob("odd.pdf", b"x", metadata={"owner_oid": "user-a"})

        response = await client.post("/kb/reindex", headers=ADMIN)

        assert response.status_code == 200
        by_name = {r["blob_name"].split("/")[-1]: r for r in response.json()["details"]}
        assert by_name["private.pdf"]["status"] == "ok"
        assert by_name["shared.pdf"]["status"] == "ok"
        assert by_name["legacy.pdf"]["status"] == "skipped: no recorded owner"
        assert by_name["odd.pdf"]["status"] == "skipped: unexpected blob name"

        acl = {c["blob_name"].split("/")[-1]: (c["oids"], c["groups"]) for c in world.ingests}
        assert acl == {"private.pdf": (["user-a"], []), "shared.pdf": (["user-a"], ["team-1"])}
        assert all("admin-1" not in c["oids"] for c in world.ingests)  # the admin does not take ownership


# ── Template ─────────────────────────────────────────────


class TestTemplateUpload:
    @pytest.fixture
    def template_path(self, tmp_path, monkeypatch):
        path = tmp_path / "templates" / "rfp_response_template.docx"
        monkeypatch.setattr(main, "_TEMPLATE_PATH", path)
        return path

    async def upload(self, client, who, data=SAMPLE_DOCX):
        return await client.post("/template/upload", files={"file": ("t.docx", data)}, headers=who)

    async def test_regular_users_cannot_replace_the_shared_template(self, client, template_path):
        assert (await self.upload(client, A)).status_code == 403
        assert not template_path.exists()

    async def test_admins_can(self, client, template_path):
        assert (await self.upload(client, ADMIN)).status_code == 200
        assert template_path.read_bytes() == SAMPLE_DOCX

    async def test_with_authentication_disabled_everyone_can(self, client, use_settings, template_path):
        use_settings("disabled")
        assert (await self.upload(client, {})).status_code == 200


# ── Input validation and limits ──────────────────────────


class TestInputValidation:
    BAD_IDENTIFIERS = ["../x", "a/b", "it's", " x", "a" * 65, "x\n"]

    @pytest.mark.parametrize("bad", [*BAD_IDENTIFIERS, ""])
    async def test_json_identifiers_with_path_or_filter_characters_are_rejected(self, client, bad):
        response = await client.post(
            "/answer", json={"question": "q", "client_id": bad, "project_id": "p"}, headers=A
        )
        assert response.status_code == 422

    @pytest.mark.parametrize("bad", BAD_IDENTIFIERS)
    async def test_form_identifiers_with_path_or_filter_characters_are_rejected(self, client, world, bad):
        for field in ("client_id", "project_id", "solution_area"):
            response = await upload_kb(client, A, **{field: bad})
            assert response.status_code == 422, f"{field}={bad!r} was accepted"
        assert world.storage.kb.blobs == {}

    async def test_an_empty_form_value_means_not_provided_and_uses_the_default(self, client, world):
        response = await upload_kb(client, A, client_id="")
        assert response.status_code == 200
        assert response.json()["client_id"] == "demo-client"

    @pytest.mark.parametrize("name", ["a/b.pdf", "..\\x.pdf"])
    async def test_file_names_with_path_components_are_rejected(self, client, name):
        assert (await client.post("/rfp/upload", files={"file": (name, b"x")}, headers=A)).status_code == 400
        assert (await upload_kb(client, A, name=name)).status_code == 400

    async def test_file_type_is_still_enforced(self, client):
        assert (await client.post("/rfp/upload", files={"file": ("x.exe", b"x")}, headers=A)).status_code == 400
        assert (await upload_kb(client, A, name="x.xlsx")).status_code == 400


class TestUploadLimit:
    async def test_files_over_the_limit_are_rejected_on_every_upload_endpoint(self, client, use_settings, world):
        use_settings("easyauth", max_upload_mb=1)
        too_big = b"x" * (1024 * 1024 + 1)

        rfp = await client.post("/rfp/upload", files={"file": ("big.csv", too_big)}, headers=A)
        kb = await upload_kb(client, A, data=too_big)
        template = await client.post("/template/upload", files={"file": ("t.docx", too_big)}, headers=ADMIN)

        assert (rfp.status_code, kb.status_code, template.status_code) == (413, 413, 413)
        assert world.storage.rfps.blobs == {} and world.storage.kb.blobs == {}

    async def test_a_file_exactly_at_the_limit_is_accepted(self, client, use_settings):
        use_settings("easyauth", max_upload_mb=1)
        response = await client.post(
            "/rfp/upload", files={"file": ("ok.csv", b"x" * (1024 * 1024))}, headers=A
        )
        assert response.status_code == 200


class TestRateLimiting:
    @pytest.fixture(autouse=True)
    def fake_agent(self, monkeypatch):
        monkeypatch.setattr("app.agents.foundry_agent.run_single_answer", lambda **kwargs: {"answer_markdown": "ok"})

    async def ask(self, client, who):
        body = {"question": "q", "client_id": "acme", "project_id": "p1"}
        return await client.post("/answer", json=body, headers=who)

    async def test_requests_over_the_limit_get_429_with_retry_after(self, client, use_settings):
        use_settings("easyauth", rate_limit_per_minute=2)

        statuses = [(await self.ask(client, A)).status_code for _ in range(3)]
        blocked = await self.ask(client, A)

        assert statuses == [200, 200, 429]
        assert int(blocked.headers["Retry-After"]) >= 1

    async def test_the_limit_is_per_user(self, client, use_settings):
        use_settings("easyauth", rate_limit_per_minute=1)

        assert (await self.ask(client, A)).status_code == 200
        assert (await self.ask(client, A)).status_code == 429
        assert (await self.ask(client, B)).status_code == 200

    async def test_zero_turns_the_limit_off(self, client, use_settings):
        use_settings("easyauth", rate_limit_per_minute=0)
        assert all([(await self.ask(client, A)).status_code == 200 for _ in range(10)])

    async def test_probes_and_listing_are_not_limited(self, client, use_settings):
        use_settings("easyauth", rate_limit_per_minute=1)
        for _ in range(5):
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/kb/list", headers=A)).status_code == 200
