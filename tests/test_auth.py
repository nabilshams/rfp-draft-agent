"""Tests for the authentication modes (disabled, easyauth, jwt) and JWKS caching."""

import base64
import json
import logging
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from app import main
from app.auth import entra
from app.config import Settings, get_settings

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT_ID = "22222222-2222-2222-2222-222222222222"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"


def make_settings(mode: str, **overrides) -> Settings:
    values = {
        "auth_mode": mode,
        "foundry_project_endpoint": "https://example.services.ai.azure.com",
        "azure_ai_search_connection_name": "search",
        "azure_storage_account_url": "https://example.blob.core.windows.net",
    }
    if mode == "jwt":
        values.update(entra_tenant_id=TENANT, entra_client_id=CLIENT_ID)
    values.update(overrides)
    return Settings(_env_file=None, **values)


# ── Settings validation ──────────────────────────────────


class TestSettingsValidation:
    def test_jwt_mode_requires_tenant_and_client(self):
        with pytest.raises(ValidationError, match="AUTH_MODE=jwt requires"):
            make_settings("jwt", entra_tenant_id="", entra_client_id="")

    def test_old_dev_tenant_value_is_rejected_with_a_hint(self):
        with pytest.raises(ValidationError, match="AUTH_MODE=disabled"):
            make_settings("jwt", entra_tenant_id="dev")

    def test_disabled_and_easyauth_need_no_entra_settings(self):
        assert make_settings("disabled").auth_mode == "disabled"
        assert make_settings("easyauth").auth_mode == "easyauth"

    def test_unknown_mode_is_rejected(self):
        with pytest.raises(ValidationError):
            make_settings("bypass")


# ── disabled mode ────────────────────────────────────────


class TestDisabledMode:
    async def test_returns_a_development_user(self):
        user = await entra.authenticate({}, make_settings("disabled"))
        assert user.oid == "dev-user-oid"

    async def test_each_call_gets_its_own_copy(self):
        settings = make_settings("disabled")
        first = await entra.authenticate({}, settings)
        first.groups.append("mutated")
        second = await entra.authenticate({}, settings)
        assert "mutated" not in second.groups

    def test_startup_logs_a_warning_when_disabled(self, caplog):
        with caplog.at_level(logging.INFO, logger="app.auth.entra"):
            entra.log_auth_mode(make_settings("disabled"))
        assert any(r.levelno == logging.WARNING and "AUTHENTICATION IS DISABLED" in r.message for r in caplog.records)

    def test_startup_does_not_warn_when_auth_is_enforced(self, caplog):
        with caplog.at_level(logging.INFO, logger="app.auth.entra"):
            entra.log_auth_mode(make_settings("easyauth"))
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


# ── easyauth mode ────────────────────────────────────────


def principal_header(claims: list[dict] | None = None, **extra) -> dict[str, str]:
    body = {"auth_typ": "aad", "claims": claims if claims is not None else []}
    encoded = base64.b64encode(json.dumps(body).encode()).decode()
    return {"x-ms-client-principal": encoded, **extra}


class TestEasyAuthMode:
    settings = make_settings("easyauth")

    async def test_builds_user_from_principal_claims(self):
        headers = principal_header(
            [
                {"typ": "oid", "val": "user-1"},
                {"typ": "name", "val": "Ada Lovelace"},
                {"typ": "preferred_username", "val": "ada@contoso.com"},
                {"typ": "groups", "val": "group-a"},
                {"typ": "groups", "val": "group-b"},
            ]
        )
        user = await entra.authenticate(headers, self.settings)
        assert user.oid == "user-1"
        assert user.name == "Ada Lovelace"
        assert user.preferred_username == "ada@contoso.com"
        assert user.groups == ["group-a", "group-b"]

    async def test_accepts_the_long_form_object_id_claim(self):
        claims = [{"typ": "http://schemas.microsoft.com/identity/claims/objectidentifier", "val": "user-2"}]
        user = await entra.authenticate(principal_header(claims), self.settings)
        assert user.oid == "user-2"

    async def test_falls_back_to_the_principal_id_header(self):
        headers = principal_header([], **{"x-ms-client-principal-id": "user-3", "x-ms-client-principal-name": "u3@x.com"})
        user = await entra.authenticate(headers, self.settings)
        assert user.oid == "user-3"
        assert user.preferred_username == "u3@x.com"

    async def test_principal_id_header_alone_is_enough(self):
        user = await entra.authenticate({"x-ms-client-principal-id": "user-4"}, self.settings)
        assert user.oid == "user-4"
        assert user.groups == []

    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"x-ms-client-principal": "not base64!!"},
            {"x-ms-client-principal": base64.b64encode(b"not json").decode()},
            {"x-ms-client-principal": base64.b64encode(b'{"no_claims": true}').decode()},
            {"x-ms-client-principal": base64.b64encode(b'{"claims": "oops"}').decode()},
            {"x-ms-client-principal": base64.b64encode(b"[1, 2]").decode()},
            principal_header([{"typ": "name", "val": "No Oid"}]),
        ],
    )
    async def test_missing_or_invalid_identity_is_rejected(self, headers):
        with pytest.raises(HTTPException) as exc:
            await entra.authenticate(headers, self.settings)
        assert exc.value.status_code == 401


# ── jwt mode ─────────────────────────────────────────────


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks_for(private_key, kid="key-1") -> dict:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update(kid=kid, use="sig", alg="RS256")
    return {"keys": [jwk]}


def make_token(private_key, *, kid="key-1", **claims) -> str:
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "exp": now + 600,
        "nbf": now - 10,
        "oid": "user-1",
        "name": "Ada Lovelace",
        "preferred_username": "ada@contoso.com",
        "groups": ["group-a", "group-b"],
    }
    payload.update(claims)
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, private_key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def serve_jwks(monkeypatch, signing_key):
    """Make the app fetch our test key set instead of calling Entra ID."""

    def _serve(jwks):
        async def fake_get_jwks(tenant_id, *, force_refresh=False):
            return jwks

        monkeypatch.setattr(entra, "_get_jwks", fake_get_jwks)

    _serve(jwks_for(signing_key))
    return _serve


class TestJwtMode:
    settings = make_settings("jwt")

    async def authenticate(self, token):
        return await entra.authenticate({"authorization": f"Bearer {token}"}, self.settings)

    async def test_valid_token_is_accepted(self, signing_key, serve_jwks):
        user = await self.authenticate(make_token(signing_key))
        assert user.oid == "user-1"
        assert user.groups == ["group-a", "group-b"]
        assert user.name == "Ada Lovelace"
        assert user.preferred_username == "ada@contoso.com"

    async def test_explicit_audience_overrides_client_id(self, signing_key, serve_jwks):
        settings = make_settings("jwt", entra_audience="api://custom")
        token = make_token(signing_key, aud="api://custom")
        user = await entra.authenticate({"authorization": f"Bearer {token}"}, settings)
        assert user.oid == "user-1"

    @pytest.mark.parametrize(
        ("claims", "detail"),
        [
            ({"exp": int(time.time()) - 60}, "Token expired"),
            ({"aud": "someone-else"}, "Invalid token"),
            ({"iss": "https://login.microsoftonline.com/other-tenant/v2.0"}, "Invalid token"),
            ({"oid": None}, "no oid claim"),
        ],
    )
    async def test_invalid_claims_are_rejected(self, signing_key, serve_jwks, claims, detail):
        with pytest.raises(HTTPException) as exc:
            await self.authenticate(make_token(signing_key, **claims))
        assert exc.value.status_code == 401
        assert detail in exc.value.detail

    async def test_token_signed_with_another_key_is_rejected(self, serve_jwks):
        attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with pytest.raises(HTTPException) as exc:
            await self.authenticate(make_token(attacker_key))  # same kid, different key
        assert exc.value.status_code == 401

    async def test_garbage_token_is_a_401_not_a_server_error(self, serve_jwks):
        with pytest.raises(HTTPException) as exc:
            await self.authenticate("not.a.jwt")
        assert exc.value.status_code == 401

    @pytest.mark.parametrize("headers", [{}, {"authorization": "Basic abc"}, {"authorization": "Bearer "}])
    async def test_missing_or_malformed_authorization_header(self, headers):
        with pytest.raises(HTTPException) as exc:
            await entra.authenticate(headers, self.settings)
        assert exc.value.status_code == 401
        assert exc.value.headers["WWW-Authenticate"] == "Bearer"

    async def test_rotated_key_is_picked_up_by_refetching_once(self, monkeypatch, signing_key):
        new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        fetches = []

        async def fake_get_jwks(tenant_id, *, force_refresh=False):
            fetches.append(force_refresh)
            return jwks_for(new_key, "key-2") if force_refresh else jwks_for(signing_key, "key-1")

        monkeypatch.setattr(entra, "_get_jwks", fake_get_jwks)

        user = await self.authenticate(make_token(new_key, kid="key-2"))

        assert user.oid == "user-1"
        assert fetches == [False, True]

    async def test_unknown_key_is_rejected_after_one_refetch(self, monkeypatch, signing_key):
        fetches = []

        async def fake_get_jwks(tenant_id, *, force_refresh=False):
            fetches.append(force_refresh)
            return jwks_for(signing_key, "key-1")

        monkeypatch.setattr(entra, "_get_jwks", fake_get_jwks)

        with pytest.raises(HTTPException) as exc:
            await self.authenticate(make_token(signing_key, kid="unknown"))

        assert exc.value.status_code == 401
        assert fetches == [False, True]

    async def test_unreachable_key_endpoint_is_a_503(self, monkeypatch, signing_key):
        async def failing_get_jwks(tenant_id, *, force_refresh=False):
            raise httpx.ConnectError("no network")

        monkeypatch.setattr(entra, "_get_jwks", failing_get_jwks)

        with pytest.raises(HTTPException) as exc:
            await self.authenticate(make_token(signing_key))
        assert exc.value.status_code == 503


# ── JWKS cache ───────────────────────────────────────────


class FakeHttpClient:
    """Stands in for httpx.AsyncClient; counts requests and can be told to fail."""

    calls = 0
    fail = False
    keys = {"keys": [{"kid": "k"}]}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url, **_kwargs):
        type(self).calls += 1
        if type(self).fail:
            raise httpx.ConnectError("no network")
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: type(self).keys)


@pytest.fixture
def jwks_http(monkeypatch):
    FakeHttpClient.calls = 0
    FakeHttpClient.fail = False
    monkeypatch.setattr(entra, "_jwks_cache", None)
    monkeypatch.setattr(entra, "_jwks_fetched_at", 0.0)
    monkeypatch.setattr(entra.httpx, "AsyncClient", FakeHttpClient)
    clock = {"now": 10_000.0}
    monkeypatch.setattr(entra, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    return clock


class TestJwksCache:
    async def test_keys_are_cached_until_the_ttl_expires(self, jwks_http):
        await entra._get_jwks(TENANT)
        await entra._get_jwks(TENANT)
        assert FakeHttpClient.calls == 1

        jwks_http["now"] += entra._JWKS_TTL_SECONDS + 1
        await entra._get_jwks(TENANT)
        assert FakeHttpClient.calls == 2

    async def test_forced_refresh_is_rate_limited(self, jwks_http):
        await entra._get_jwks(TENANT)

        await entra._get_jwks(TENANT, force_refresh=True)  # too soon: served from cache
        assert FakeHttpClient.calls == 1

        jwks_http["now"] += entra._JWKS_MIN_REFRESH_SECONDS + 1
        await entra._get_jwks(TENANT, force_refresh=True)
        assert FakeHttpClient.calls == 2

    async def test_stale_keys_are_used_when_the_refresh_fails(self, jwks_http):
        first = await entra._get_jwks(TENANT)
        jwks_http["now"] += entra._JWKS_TTL_SECONDS + 1
        FakeHttpClient.fail = True

        assert await entra._get_jwks(TENANT) == first

    async def test_failure_without_cached_keys_is_raised(self, jwks_http):
        FakeHttpClient.fail = True
        with pytest.raises(httpx.ConnectError):
            await entra._get_jwks(TENANT)


# ── HTTP and WebSocket surfaces ──────────────────────────


@pytest.fixture
def use_settings():
    def _use(settings):
        main.app.dependency_overrides[get_settings] = lambda: settings

    yield _use
    main.app.dependency_overrides.clear()


@pytest.fixture
def http_client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")


class TestHttpEndpoints:
    async def test_metrics_requires_authentication(self, use_settings, http_client):
        use_settings(make_settings("easyauth"))
        async with http_client as client:
            denied = await client.get("/metrics")
            allowed = await client.get("/metrics", headers=principal_header([{"typ": "oid", "val": "u"}]))

        assert denied.status_code == 401
        assert allowed.status_code == 200

    async def test_health_stays_open_for_probes(self, use_settings, http_client):
        use_settings(make_settings("jwt"))
        async with http_client as client:
            response = await client.get("/health")
        assert response.status_code == 200

    async def test_bearer_token_is_enforced_end_to_end(self, use_settings, http_client, signing_key, serve_jwks):
        use_settings(make_settings("jwt"))
        async with http_client as client:
            no_token = await client.get("/metrics")
            bad_token = await client.get("/metrics", headers={"Authorization": "Bearer nope"})
            good_token = await client.get("/metrics", headers={"Authorization": f"Bearer {make_token(signing_key)}"})

        assert no_token.status_code == 401
        assert bad_token.status_code == 401
        assert good_token.status_code == 200

    async def test_data_endpoints_are_protected(self, use_settings, http_client):
        use_settings(make_settings("easyauth"))
        async with http_client as client:
            response = await client.get("/kb/list")
        assert response.status_code == 401


class TestWebSocketLogs:
    def test_unauthenticated_connection_is_closed(self, use_settings):
        use_settings(make_settings("easyauth"))
        with pytest.raises(WebSocketDisconnect) as exc, TestClient(main.app).websocket_connect("/ws/logs"):
            pass
        assert exc.value.code == 1008

    def test_authenticated_connection_is_accepted(self, use_settings):
        use_settings(make_settings("easyauth"))
        headers = principal_header([{"typ": "oid", "val": "u"}])
        with TestClient(main.app).websocket_connect("/ws/logs", headers=headers) as ws:
            ws.close()

    def test_connection_is_accepted_when_auth_is_disabled(self, use_settings):
        use_settings(make_settings("disabled"))
        with TestClient(main.app).websocket_connect("/ws/logs") as ws:
            ws.close()
