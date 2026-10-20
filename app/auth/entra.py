"""Entra ID (Azure AD) JWT validation and claims extraction.

Validates Bearer tokens issued by Microsoft Entra ID and extracts the
user's *oid* and *groups* claims for downstream DLAC enforcement.
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_bearer_scheme = HTTPBearer(auto_error=False)

# Cache JWKS keys in-process
_jwks_cache: dict | None = None


class UserClaims(BaseModel):
    """Minimal validated identity claims needed for DLAC."""

    oid: str
    groups: list[str] = []
    name: str = ""
    preferred_username: str = ""


async def _get_jwks(tenant_id: str) -> dict:
    """Fetch and cache JWKS from the Entra ID v2 endpoint."""
    global _jwks_cache
    if _jwks_cache is not None:
        return _jwks_cache
    url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, timeout=10)
        resp.raise_for_status()
        _jwks_cache = resp.json()
        return _jwks_cache


async def _validate_token(token: str, settings: Settings) -> UserClaims:
    """Decode and validate the JWT against Entra ID public keys."""
    jwks = await _get_jwks(settings.entra_tenant_id)

    # Determine the signing key
    unverified_header = jwt.get_unverified_header(token)
    kid = unverified_header.get("kid")
    rsa_key: dict = {}
    for key in jwks.get("keys", []):
        if key["kid"] == kid:
            rsa_key = key
            break
    if not rsa_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unable to find signing key")

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
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f"Invalid token: {exc}")

    return UserClaims(
        oid=payload.get("oid", ""),
        groups=payload.get("groups", []),
        name=payload.get("name", ""),
        preferred_username=payload.get("preferred_username", ""),
    )


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
    settings: Settings = Depends(get_settings),
) -> UserClaims:
    """FastAPI dependency – returns validated :class:`UserClaims`.

    In development (``ENTRA_TENANT_ID`` set to ``dev``), authentication is
    bypassed and a synthetic user is returned.
    """
    # Dev bypass
    if settings.entra_tenant_id == "dev":
        return UserClaims(
            oid="dev-user-oid",
            groups=["dev-group"],
            name="Dev User",
            preferred_username="dev@localhost",
        )

    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Authorization header")

    return await _validate_token(credentials.credentials, settings)
