"""A minimal OAuth 2.1 authorization server whose only "account" is one password.

claude.ai drives the full OAuth dance (PKCE, Dynamic Client Registration, the
``.well-known`` metadata, ``/authorize`` + ``/token``) — the MCP SDK mounts all
of that for us. We only supply the provider behind it, and the one human-facing
step is a single password form (see :mod:`weight_mcp.web`).

Design choices that keep this stateless and restart-safe:

* Access/refresh tokens are **JWTs signed with a key derived from the password**.
  Nothing to store; they survive restarts; rotating the password invalidates
  every token (that *is* our "revoke everything").
* Authorization codes are ephemeral (5 min) so they live in memory.
* DCR clients are the one durable thing claude.ai holds long-term, so those are
  persisted in SQLite.
"""

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any

import jwt
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .db import Database

ACCESS_TTL = 3600  # 1 hour
REFRESH_TTL = 30 * 24 * 3600  # 30 days
CODE_TTL = 300  # 5 minutes
SCOPE = "user"
_OWNER = "owner"  # the single subject; there is only ever one user


@dataclass(slots=True)
class PendingAuthorization:
    """An in-flight ``/authorize`` request, waiting on the password form."""

    client_id: str
    params: AuthorizationParams


@dataclass(slots=True)
class TokenClaims:
    """The subset of JWT claims this server cares about."""

    typ: str
    client_id: str
    expires_at: int


def _signing_key(password: str) -> bytes:
    """Derive a stable JWT key from the password (changes when the password does)."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), b"weight-mcp-oauth", 100_000)


class PasswordOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(self, *, password: str, resource_url: str, login_path: str, db: Database) -> None:
        self._password = password
        self._key = _signing_key(password)
        self._resource = resource_url
        self._login_path = login_path
        self._db = db
        self._pending: dict[str, PendingAuthorization] = {}
        self._codes: dict[str, AuthorizationCode] = {}

    # --- clients (persisted) ------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info_json = self._db.get_oauth_client(client_id)
        if info_json is None:
            return None
        return OAuthClientInformationFull.model_validate_json(info_json)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.client_id:
            self._db.add_oauth_client(client_info.client_id, client_info.model_dump_json())

    # --- authorize: hand off to the password form ---------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        txn = secrets.token_urlsafe(24)
        self._pending[txn] = PendingAuthorization(client_id=client.client_id or "", params=params)
        return f"{self._login_path}?txn={txn}"

    def password_ok(self, password: str) -> bool:
        return hmac.compare_digest(password, self._password)

    def pending_exists(self, txn: str) -> bool:
        return txn in self._pending

    def complete_login(self, txn: str) -> str | None:
        """Consume a verified login: mint an auth code and return the redirect URL."""
        pending = self._pending.pop(txn, None)
        if pending is None:
            return None
        params = pending.params
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=[SCOPE],
            expires_at=time.time() + CODE_TTL,
            client_id=pending.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=_OWNER,
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    # --- code + token exchange ---------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None or code.expires_at < time.time():
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        return self._issue(client.client_id or "")

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        claims = self._decode(refresh_token, expected="refresh")
        if claims is None or claims.client_id != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id or "",
            scopes=[SCOPE],
            expires_at=claims.expires_at,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotating refresh tokens, as required for public clients by OAuth 2.1.
        return self._issue(client.client_id or "")

    async def load_access_token(self, token: str) -> AccessToken | None:
        claims = self._decode(token, expected="access", audience=self._resource)
        if claims is None:
            return None
        return AccessToken(
            token=token,
            client_id=claims.client_id,
            scopes=[SCOPE],
            expires_at=claims.expires_at,
            resource=self._resource,
            subject=_OWNER,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Stateless tokens can't be individually revoked; rotate the password to
        # invalidate every token at once.
        return None

    # --- jwt helpers --------------------------------------------------------

    def _issue(self, client_id: str) -> OAuthToken:
        return OAuthToken(
            access_token=self._encode("access", client_id, ACCESS_TTL, audience=self._resource),
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            scope=SCOPE,
            refresh_token=self._encode("refresh", client_id, REFRESH_TTL),
        )

    def _encode(self, typ: str, client_id: str, ttl: int, *, audience: str | None = None) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": _OWNER,
            "cid": client_id,
            "typ": typ,
            "iat": now,
            "exp": now + ttl,
        }
        if audience is not None:
            payload["aud"] = audience
        return jwt.encode(payload, self._key, algorithm="HS256")

    def _decode(
        self, token: str, *, expected: str, audience: str | None = None
    ) -> TokenClaims | None:
        try:
            payload = jwt.decode(
                token,
                self._key,
                algorithms=["HS256"],
                audience=audience,
                options={"require": ["exp"], "verify_aud": audience is not None},
            )
        except jwt.PyJWTError:
            return None
        if payload.get("typ") != expected:
            return None
        return TokenClaims(
            typ=expected,
            client_id=str(payload.get("cid", "")),
            expires_at=int(payload["exp"]),
        )
