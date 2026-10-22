"""When Azure refuses the app or can't be reached, the user gets a clear 503, not a bare 500 or a misleading 404."""

import logging

import pytest
from azure.core.exceptions import (
    ClientAuthenticationError,
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
)

from app import azure_clients, health
from app.agents import lifecycle
from app.config import Settings

from .test_authorization_api import A, make_settings, upload_rfp

NOT_AUTHORIZED = "This request is not authorized to perform this operation."


class FailingStorage:
    """Every call to the container raises the given error."""

    def __init__(self, error):
        self.error = error

    def __getattr__(self, _name):
        def fail(*_args, **_kwargs):
            raise self.error

        return fail


def refused(status=403):
    response = type("R", (), {"status_code": status, "reason": "x", "headers": {}, "text": lambda self: ""})()
    return HttpResponseError(message=NOT_AUTHORIZED, response=response)


@pytest.fixture
def storage_fails(monkeypatch, client, world):
    def _fail(error):
        monkeypatch.setattr(azure_clients, "container", lambda _s, _n: FailingStorage(error))

    return _fail


class TestClassification:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (refused(403), health.REFUSED),
            (refused(401), health.REFUSED),
            (ClientAuthenticationError(message="no token"), health.REFUSED),
            (ServiceRequestError(message="name resolution failed"), health.UNREACHABLE),
            (refused(500), health.ERROR),
            (RuntimeError("boom"), health.ERROR),
        ],
    )
    def test_azure_errors_are_sorted_into_kinds(self, error, expected):
        assert health.classify(error) == expected


class TestApiErrors:
    async def test_a_refused_storage_call_is_a_clear_503(self, client, storage_fails, caplog):
        storage_fails(refused(403))
        with caplog.at_level(logging.ERROR):
            response = await client.get("/kb/list", headers=A)

        assert response.status_code == 503
        assert "can't reach one of its Azure services" in response.json()["detail"]
        assert NOT_AUTHORIZED not in response.text  # detail stays in the log
        assert any(NOT_AUTHORIZED in r.getMessage() for r in caplog.records)

    async def test_an_unreachable_service_is_a_503_too(self, client, storage_fails):
        storage_fails(ServiceRequestError(message="Failed to resolve st.blob.core.windows.net"))
        assert (await client.get("/kb/list", headers=A)).status_code == 503

    async def test_other_azure_errors_are_a_502(self, client, storage_fails):
        storage_fails(refused(500))
        response = await client.get("/kb/list", headers=A)
        assert response.status_code == 502 and "Azure service returned an error" in response.json()["detail"]

    async def test_uploads_report_the_same_problem(self, client, storage_fails):
        storage_fails(refused(403))
        response = await client.post("/rfp/upload", files={"file": ("rfp.docx", b"x")}, headers=A)
        assert response.status_code == 503

    async def test_a_storage_refusal_is_not_reported_as_a_missing_rfp(self, client, world, monkeypatch):
        rfp_id = await upload_rfp(client, A)
        container = world.storage.rfps

        def refuse(*_a, **_kw):
            raise refused(403)

        monkeypatch.setattr(container, "download_blob", refuse)
        response = await client.post("/rfp/extract", json={"rfp_id": rfp_id, "filename": "rfp.docx"}, headers=A)
        assert response.status_code == 503  # not a 404

    async def test_a_genuinely_missing_blob_is_still_a_404(self, client, world, monkeypatch):
        rfp_id = await upload_rfp(client, A)

        def missing(*_a, **_kw):
            raise ResourceNotFoundError("gone")

        monkeypatch.setattr(world.storage.rfps, "download_blob", missing)
        response = await client.post("/rfp/extract", json={"rfp_id": rfp_id, "filename": "rfp.docx"}, headers=A)
        assert response.status_code == 404

    async def test_deleting_an_already_missing_blob_still_cleans_the_index(self, client, world, monkeypatch):
        from .test_authorization_api import ADMIN

        world.storage.kb.upload_blob("acme/p1/general/guide.pdf", b"x", metadata={"owner_oid": "user-a"})

        def missing(*_a, **_kw):
            raise ResourceNotFoundError("gone")

        monkeypatch.setattr(world.storage.kb, "delete_blob", missing)
        response = await client.delete("/kb/delete", params={"blob_name": "acme/p1/general/guide.pdf"}, headers=ADMIN)
        assert response.status_code == 200 and response.json()["deleted_chunks"] == 2


class TestDependencyChecks:
    @pytest.fixture
    def settings(self) -> Settings:
        return make_settings()

    def patch_checks(self, monkeypatch, **outcomes):
        def check_for(outcome):
            def check(_settings):
                if outcome is not None:
                    raise outcome

            return check

        monkeypatch.setattr(health, "CHECKS", {name: check_for(o) for name, o in outcomes.items()})

    def test_all_ok(self, monkeypatch, settings):
        self.patch_checks(monkeypatch, storage=None, search=None, agents=None)
        assert health.check_dependencies(settings) == {"storage": "ok", "search": "ok", "agents": "ok"}

    def test_each_failure_is_reported_separately_and_nothing_raises(self, monkeypatch, settings):
        self.patch_checks(
            monkeypatch, storage=refused(403), search=ServiceRequestError(message="dns"), agents=RuntimeError("x")
        )
        assert health.check_dependencies(settings) == {
            "storage": "refused",
            "search": "unreachable",
            "agents": "error",
        }

    async def test_the_endpoint_reports_status_without_details(self, client, monkeypatch):
        self.patch_checks(monkeypatch, storage=refused(403), search=None, agents=None)
        response = await client.get("/health/dependencies", headers=A)
        assert response.status_code == 200
        assert response.json() == {
            "status": "degraded",
            "dependencies": {"storage": "refused", "search": "ok", "agents": "ok"},
        }
        assert NOT_AUTHORIZED not in response.text

    async def test_the_endpoint_says_ok_when_everything_works(self, client, monkeypatch):
        self.patch_checks(monkeypatch, storage=None, search=None)
        assert (await client.get("/health/dependencies", headers=A)).json()["status"] == "ok"

    async def test_the_endpoint_requires_sign_in(self, client):
        assert (await client.get("/health/dependencies")).status_code == 401

    def test_the_real_checks_call_the_expected_services(self, monkeypatch, settings):
        calls = []
        monkeypatch.setattr(
            azure_clients,
            "container",
            lambda _s, name: type("C", (), {"get_container_properties": lambda self: calls.append(("storage", name))})(),
        )
        monkeypatch.setattr(
            azure_clients,
            "search_index_client",
            lambda _s: type("I", (), {"list_index_names": lambda self: calls.append(("search",)) or iter(())})(),
        )
        monkeypatch.setattr(
            azure_clients,
            "agents_client",
            lambda _s: type("A", (), {"list_agents": lambda self, limit: calls.append(("agents", limit)) or iter(())})(),
        )
        assert set(health.check_dependencies(settings).values()) == {"ok"}
        assert calls == [("storage", "rfp-uploads"), ("search",), ("agents", 1)]


class TestStartupLogging:
    def test_a_failed_check_is_logged_as_an_error_with_the_outcome(self, monkeypatch, caplog):
        from app import main

        monkeypatch.setattr(health, "check_dependencies", lambda _s: {"storage": "refused", "search": "ok"})
        with caplog.at_level(logging.INFO):
            main._log_dependency_status(make_settings())
        record = next(r for r in caplog.records if "Dependency status" in r.getMessage())
        assert record.levelno == logging.ERROR
        assert "storage=refused" in record.getMessage()

    def test_all_ok_is_logged_at_info(self, monkeypatch, caplog):
        from app import main

        monkeypatch.setattr(health, "check_dependencies", lambda _s: {"storage": "ok"})
        with caplog.at_level(logging.INFO):
            main._log_dependency_status(make_settings())
        assert next(r for r in caplog.records if "Dependency status" in r.getMessage()).levelno == logging.INFO

    def test_startup_does_not_wait_for_the_checks(self, monkeypatch):
        import threading
        import time

        from fastapi.testclient import TestClient

        from app import main

        release = threading.Event()
        monkeypatch.setattr(health, "check_dependencies", lambda _s: release.wait(5) or {})
        monkeypatch.setattr(lifecycle, "setup_agents", lambda: {})
        monkeypatch.setattr("app.telemetry.tracing.configure_tracing", lambda _app: None)

        started = time.perf_counter()
        with TestClient(main.app) as test_client:
            assert test_client.get("/health").status_code == 200
        elapsed = time.perf_counter() - started
        release.set()
        assert elapsed < 3
