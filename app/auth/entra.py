"""Authentication for the API.

``AUTH_MODE`` selects how a request is authenticated:

* ``disabled`` – no authentication; every request is a synthetic development user.
  For local development and throw-away demos only.
* ``easyauth`` – Azure Container Apps built-in authentication signs users in and passes
  their identity to the app in ``X-MS-CLIENT-PRINCIPAL*`` headers. The app trusts those
  headers, so it must only be reachable through that authentication layer.
* ``jwt`` – the app validates Entra ID bearer tokens itself (signature, issuer, audience, expiry).

In every mode the result is a :class:`UserClaims` with the user's *oid* and *groups*,
which downstream code uses for document-level access control (DLAC).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from collections.abc import Mapping
from typing import Annotated, Any

import httpx
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Only documents the bearer scheme in the OpenAPI UI; the header is read directly so the
# same logic also works for WebSocket connections.
_bearer_scheme = HTTPBearer(auto_error=False)

# Entra ID rotates signing keys, so the key set is cached for a limited time and refetched
# when a token names a key we don't have (at most once per minimum interval).
_JWKS_TTL_SECONDS = 3600
_JWKS_MIN_REFRESH_SECONDS = 60
_jwks_cache: dict | None = None
_jwks_fetched_at: float = 0.0

_PRINCIPAL_HEADER = "x-ms-client-principal"
_PRINCIPAL_ID_HEADER = "x-ms-client-principal-id"
_PRINCIPAL_NAME_HEADER = "x-ms-client-principal-name"
_OID_CLAIM_TYPES = ("oid", "http://schemas.microsoft.com/identity/claims/objectidentifier")


class UserClaims(BaseModel):
    """Minimal validated identity claims needed for DLAC."""

    oid: str
    groups: list[str] = []
    name: str = ""
    preferred_username: str = ""


_DEV_USER = UserClaims(
    oid="dev-user-oid",
    groups=["dev-group"],
    name="Dev User",
    preferred_username="dev@localhost",
)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def log_auth_mode(settings: Settings) -> None:
    """Log the active authentication mode at startup; disabled mode is logged as a warning."""
    if settings.auth_mode == "disabled":
        logger.warning(
            "AUTHENTICATION IS DISABLED (AUTH_MODE=disabled): every request is treated as a "
            "development user. Do not expose this instance to the internet or load real data."
        )
    else:
        logger.info("Authentication mode: %s", settings.auth_mode)


# ── jwt mode ─────────────────────────────────────────────


async def _get_jwks(tenant_id: str, *, force_refresh: bool = False) -> dict:
    """Return the tenant's signing keys, from cache when fresh."""
    global _jwks_cache, _jwks_fetched_at
    age = time.monotonic() - _jwks_fetched_at
    if _jwks_cache is not None:
        if force_refresh:
            if age < _JWKS_MIN_REFRESH_SECONDS:
                return _jwks_cache
        elif age < _JWKS_TTL_SECONDS:
            return _jwks_cache

    url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10)
            resp.raise_for_status()
            keys = resp.json()
    except httpx.HTTPError:
        if _jwks_cache is not None:
            logger.warning("Could not refresh Entra signing keys; using cached keys", exc_info=True)
            return _jwks_cache
        raise
    _jwks_cache = keys
    _jwks_fetched_at = time.monotonic()
    return keys


def _find_key(jwks: dict, kid: str | None) -> dict | None:
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None


async def _validate_token(token: str, settings: Settings) -> UserClaims:
    """Decode and validate the JWT against Entra ID public keys."""
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.InvalidTokenError as exc:
        raise _unauthorized(f"Invalid token: {exc}") from exc

    try:
        jwks = await _get_jwks(settings.entra_tenant_id)
        rsa_key = _find_key(jwks, kid)
        if rsa_key is None:
            # Possibly a rotated key: refetch once (rate limited) before rejecting.
            jwks = await _get_jwks(settings.entra_tenant_id, force_refresh=True)
            rsa_key = _find_key(jwks, kid)
    except httpx.HTTPError as exc:
        logger.error("Could not fetch Entra signing keys: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to retrieve token signing keys",
        ) from exc
    if rsa_key is None:
        raise _unauthorized("Unable to find signing key")

    audience = settings.entra_audience or settings.entra_client_id
    issuer = f"https://login.microsoftonline.com/{settings.entra_tenant_id}/v2.0"

    try:
        payload = jwt.decode(
            token,
            jwt.algorithms.RSAAlgorithm.from_jwk(rsa_key),
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"verify_exp": True},
        )
    except jwt.ExpiredSignatureError as exc:
        raise _unauthorized("Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise _unauthorized(f"Invalid token: {exc}") from exc

    oid = payload.get("oid", "")
    if not oid:
        raise _unauthorized("Token has no oid claim")

    return UserClaims(
        oid=oid,
        groups=payload.get("groups", []),
        name=payload.get("name", ""),
        preferred_username=payload.get("preferred_username", ""),
    )


# ── easyauth mode ────────────────────────────────────────


def _user_from_easy_auth_headers(headers: Mapping[str, str]) -> UserClaims:
    """Build the user from the identity headers set by Container Apps authentication.

    External requests cannot set these headers (the platform removes them), so they are
    trusted. ``X-MS-CLIENT-PRINCIPAL-ID`` alone is enough to identify the user; the full
    claims header, when sent, additionally provides groups and display names.
    """
    encoded = headers.get(_PRINCIPAL_HEADER)
    claims: dict[str, list[str]] = {}
    if encoded:
        try:
            principal = json.loads(base64.b64decode(encoded, validate=True))
            raw_claims: list[Any] = principal["claims"]
            for claim in raw_claims:
                if isinstance(claim, dict) and isinstance(claim.get("typ"), str) and isinstance(claim.get("val"), str):
                    claims.setdefault(claim["typ"], []).append(claim["val"])
        except (binascii.Error, ValueError, KeyError, TypeError) as exc:
            raise _unauthorized("Invalid identity header") from exc

    oid = next((claims[t][0] for t in _OID_CLAIM_TYPES if t in claims), "") or headers.get(_PRINCIPAL_ID_HEADER, "")
    if not oid:
        raise _unauthorized("Identity header has no object ID" if encoded else "Not authenticated")

    return UserClaims(
        oid=oid,
        groups=claims.get("groups", []),
        name=next(iter(claims.get("name", [])), ""),
        preferred_username=next(iter(claims.get("preferred_username", [])), "")
        or headers.get(_PRINCIPAL_NAME_HEADER, ""),
    )


# ── entry points ─────────────────────────────────────────


async def authenticate(headers: Mapping[str, str], settings: Settings) -> UserClaims:
    """Authenticate a request (HTTP or WebSocket) from its headers according to ``AUTH_MODE``."""
    if settings.auth_mode == "disabled":
        return _DEV_USER.model_copy(deep=True)
    if settings.auth_mode == "easyauth":
        return _user_from_easy_auth_headers(headers)

    scheme, _, token = headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("Missing or invalid Authorization header")
    return await _validate_token(token.strip(), settings)


async def get_current_user(
    request: Request,
    _credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
    settings: Settings = Depends(get_settings),
) -> UserClaims:
    """FastAPI dependency – returns the authenticated :class:`UserClaims` or raises 401."""
    return await authenticate(request.headers, settings)
