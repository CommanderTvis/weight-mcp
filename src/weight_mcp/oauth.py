"""A minimal OAuth 2.1 authorization server with one admin and DB-backed users.

claude.ai drives the full OAuth dance (PKCE, Dynamic Client Registration, the
``.well-known`` metadata, ``/authorize`` + ``/token``) — the MCP SDK mounts all
of that for us. We only supply the provider behind it, and the one human-facing
step is a username + password form (see :mod:`weight_mcp.web`).

Accounts come in two flavors:

* The **admin** account (username ``admin``) whose password lives in the
  environment (``.env``), never in the database.
* **Non-admin** accounts, registered by the admin via MCP tools and stored in
  SQLite with salted password hashes.

Everything here is **stateless**, so it works under multiple workers, replicas,
or a restart mid-flow — there is no in-memory session to lose:

* The login ``txn`` and the authorization ``code`` are short-lived JWTs that
  carry the authorization request inside them; nothing is stored between
  ``/authorize`` → ``/login`` → ``/token``. The code additionally carries which
  user logged in.
* Access/refresh tokens are JWTs too, with the username as ``sub``. Every JWT
  is signed with a key derived from the admin password, so rotating the admin
  password invalidates everything at once. Each token also embeds a short
  digest of its user's current password (``pwv``), so updating or deregistering
  a single user invalidates just that user's tokens.
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

from .db import Database, hash_password, verify_password

ACCESS_TTL = 3600  # 1 hour
REFRESH_TTL = 30 * 24 * 3600  # 30 days
TXN_TTL = 600  # 10 minutes to type the password
CODE_TTL = 300  # 5 minutes
DASHBOARD_COOKIE_TTL = 30 * 24 * 3600  # dashboard browser session: 30 days
SCOPE = "user"
ADMIN_USERNAME = "admin"  # the fixed account behind the .env password

# Verified against when a login names an unknown user, so the response takes as
# long as a real hash check (no username oracle via timing).
_DUMMY_HASH = hash_password("dummy")


@dataclass(slots=True)
class AuthRequest:
    """An authorization request, carried inside the txn and the auth code JWTs.

    ``username`` is empty in the txn (nobody logged in yet) and set in the code
    (minted only after a successful password check)."""

    client_id: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    code_challenge: str
    resource: str | None
    state: str | None
    expires_at: int
    username: str = ""


@dataclass(slots=True)
class TokenClaims:
    """The subset of access/refresh-token JWT claims this server cares about."""

    username: str
    client_id: str
    expires_at: int


def _signing_key(password: str) -> bytes:
    """Derive a stable JWT key from the admin password (changes when it does)."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), b"weight-mcp-oauth", 100_000)


class PasswordOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    def __init__(
        self, *, admin_password: str, resource_url: str, login_path: str, db: Database
    ) -> None:
        self._admin_password = admin_password
        self._key = _signing_key(admin_password)
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

    # --- authorize: hand off to the login form ------------------------------

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

    def verify_login(self, username: str, password: str) -> bool:
        """Check a username + password against the admin account or the DB."""
        if username == ADMIN_USERNAME:
            return hmac.compare_digest(password, self._admin_password)
        stored = self._db.get_user_password_hash(username)
        if stored is None:
            verify_password(password, _DUMMY_HASH)  # constant-time-ish: no username oracle
            return False
        return verify_password(password, stored)

    def txn_valid(self, txn: str) -> bool:
        return self._decode_request(txn, "txn") is not None

    def complete_login(self, txn: str, username: str) -> str | None:
        """Consume a verified login: mint an auth code for ``username`` and
        return the redirect URL. The caller must have checked the password."""
        request = self._decode_request(txn, "txn")
        if request is None:
            return None
        request.username = username
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
        if not request.username or self._password_version(request.username) is None:
            return None  # no login recorded, or the user was deregistered meanwhile
        return AuthorizationCode(
            code=authorization_code,
            scopes=[SCOPE],
            expires_at=request.expires_at,
            client_id=request.client_id,
            code_challenge=request.code_challenge,
            redirect_uri=AnyUrl(request.redirect_uri),
            redirect_uri_provided_explicitly=request.redirect_uri_provided_explicitly,
            resource=request.resource,
            subject=request.username,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        return self._issue(client.client_id or "", authorization_code.subject or "")

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
            subject=claims.username,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotating refresh tokens, as required for public clients by OAuth 2.1.
        return self._issue(client.client_id or "", refresh_token.subject or "")

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
            subject=claims.username,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Stateless tokens can't be individually revoked; update the user's
        # password (or rotate the admin password) to invalidate their tokens.
        return None

    # --- dashboard browser session (cookie) ---------------------------------

    def dashboard_cookie(self, username: str) -> str:
        """A signed cookie value granting dashboard access after a password login."""
        return self._encode_token("dash", username, "", DASHBOARD_COOKIE_TTL)

    def dashboard_cookie_user(self, cookie: str) -> str | None:
        """The username a dashboard cookie grants access as, or None if invalid."""
        claims = self._decode_token(cookie, expected="dash")
        return claims.username if claims else None

    # --- jwt helpers --------------------------------------------------------

    def _password_version(self, username: str) -> str | None:
        """A short digest of the user's *current* password, embedded in tokens
        so changing that password (or deregistering the user) invalidates them.
        None means the user does not exist."""
        if username == ADMIN_USERNAME:
            # The signing key already rotates with the admin password; a stable
            # marker is enough to say "this account always exists".
            return "env"
        stored = self._db.get_user_password_hash(username)
        if stored is None:
            return None
        return hashlib.sha256(stored.encode()).hexdigest()[:16]

    def _issue(self, client_id: str, username: str) -> OAuthToken:
        return OAuthToken(
            access_token=self._encode_token(
                "access", username, client_id, ACCESS_TTL, audience=self._resource
            ),
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            scope=SCOPE,
            refresh_token=self._encode_token("refresh", username, client_id, REFRESH_TTL),
        )

    def _encode_token(
        self, typ: str, username: str, client_id: str, ttl: int, *, audience: str | None = None
    ) -> str:
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": username,
            "cid": client_id,
            "typ": typ,
            "pwv": self._password_version(username) or "",
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
        username = str(payload.get("sub", ""))
        current = self._password_version(username)
        if not username or current is None:
            return None  # user deregistered (or a pre-multi-user token)
        if not hmac.compare_digest(str(payload.get("pwv", "")), current):
            return None  # password updated since this token was issued
        return TokenClaims(
            username=username,
            client_id=str(payload.get("cid", "")),
            expires_at=int(payload["exp"]),
        )

    def _encode_request(self, typ: str, request: AuthRequest) -> str:
        payload: dict[str, Any] = {
            "typ": typ,
            "cid": request.client_id,
            "ru": request.redirect_uri,
            "rupe": request.redirect_uri_provided_explicitly,
            "cc": request.code_challenge,
            "res": request.resource,
            "st": request.state,
            "sub": request.username,
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
            username=str(payload.get("sub", "")),
        )
