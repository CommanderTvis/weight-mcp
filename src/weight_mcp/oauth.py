"""A minimal OAuth 2.1 authorization server whose only "account" is one password.

claude.ai drives the full OAuth dance (PKCE, Dynamic Client Registration, the
``.well-known`` metadata, ``/authorize`` + ``/token``) — the MCP SDK mounts all
of that for us. We only supply the provider behind it, and the one human-facing
step is a single password form (see :mod:`weight_mcp.web`).

Everything here is **stateless**, so it works under multiple workers, replicas,
or a restart mid-flow — there is no in-memory session to lose:

* The login ``txn`` and the authorization ``code`` are short-lived JWTs that
  carry the authorization request inside them; nothing is stored between
  ``/authorize`` → ``/login`` → ``/token``.
* Access/refresh tokens are JWTs too. Every JWT is signed with a key derived
  from the password, so rotating the password invalidates everything at once.
* DCR clients are the one durable thing claude.ai holds long-term, so those are
  persisted in SQLite.
"""

import hashlib
import hmac
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
from pydantic import AnyUrl

from .db import Database

ACCESS_TTL = 3600  # 1 hour
REFRESH_TTL = 30 * 24 * 3600  # 30 days
TXN_TTL = 600  # 10 minutes to type the password
CODE_TTL = 300  # 5 minutes
SCOPE = "user"
_OWNER = "owner"  # the single subject; there is only ever one user


@dataclass(slots=True)
class AuthRequest:
    """An authorization request, carried inside the txn and the auth code JWTs."""

    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    resource: str | None
    state: str | None
    expires_at: int


@dataclass(slots=True)
class TokenClaims:
    """The subset of access/refresh-token JWT claims this server cares about."""

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
        txn = self._encode_request(
            "txn",
            AuthRequest(
                client_id=client.client_id or "",
                redirect_uri=str(params.redirect_uri),
                redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                code_challenge=params.code_challenge,
                resource=params.resource,
                state=params.state,
                expires_at=int(time.time()) + TXN_TTL,
            ),
        )
        return f"{self._login_path}?txn={txn}"

    def password_ok(self, password: str) -> bool:
        return hmac.compare_digest(password, self._password)

    def txn_valid(self, txn: str) -> bool:
        return self._decode_request(txn, "txn") is not None

    def complete_login(self, txn: str) -> str | None:
        """Consume a verified login: mint an auth code and return the redirect URL."""
        request = self._decode_request(txn, "txn")
        if request is None:
            return None
        request.expires_at = int(time.time()) + CODE_TTL
        code = self._encode_request("code", request)
        return construct_redirect_uri(request.redirect_uri, code=code, state=request.state)

    # --- code + token exchange ---------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        request = self._decode_request(authorization_code, "code")
        if request is None or request.client_id != (client.client_id or ""):
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=[SCOPE],
            expires_at=request.expires_at,
            client_id=request.client_id,
            code_challenge=request.code_challenge,
            redirect_uri=AnyUrl(request.redirect_uri),
            redirect_uri_provided_explicitly=request.redirect_uri_provided_explicitly,
            resource=request.resource,
            subject=_OWNER,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        return self._issue(client.client_id or "")

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        claims = self._decode_token(refresh_token, expected="refresh")
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
        claims = self._decode_token(token, expected="access", audience=self._resource)
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
            access_token=self._encode_token(
                "access", client_id, ACCESS_TTL, audience=self._resource
            ),
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            scope=SCOPE,
            refresh_token=self._encode_token("refresh", client_id, REFRESH_TTL),
        )

    def _encode_token(
        self, typ: str, client_id: str, ttl: int, *, audience: str | None = None
    ) -> str:
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

    def _decode_token(
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
        return TokenClaims(client_id=str(payload.get("cid", "")), expires_at=int(payload["exp"]))

    def _encode_request(self, typ: str, request: AuthRequest) -> str:
        payload: dict[str, Any] = {
            "typ": typ,
            "cid": request.client_id,
            "ru": request.redirect_uri,
            "rupe": request.redirect_uri_provided_explicitly,
            "cc": request.code_challenge,
            "res": request.resource,
            "st": request.state,
            "exp": request.expires_at,
        }
        return jwt.encode(payload, self._key, algorithm="HS256")

    def _decode_request(self, token: str, expected: str) -> AuthRequest | None:
        try:
            payload = jwt.decode(
                token, self._key, algorithms=["HS256"], options={"require": ["exp"]}
            )
        except jwt.PyJWTError:
            return None
        if payload.get("typ") != expected:
            return None
        return AuthRequest(
            client_id=str(payload.get("cid", "")),
            redirect_uri=str(payload.get("ru", "")),
            redirect_uri_provided_explicitly=bool(payload.get("rupe", False)),
            code_challenge=str(payload.get("cc", "")),
            resource=payload.get("res"),
            state=payload.get("st"),
            expires_at=int(payload["exp"]),
        )
