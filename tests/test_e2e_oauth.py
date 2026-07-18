"""End-to-end OAuth + MCP flow through the SDK's mounted routes (no network).

Exercises the whole connector handshake the way claude.ai would: Dynamic Client
Registration, the PKCE authorize redirect, the login gate, the token exchange,
and finally-authenticated MCP requests — including the admin registering users
and each account seeing only its own data.
"""

import base64
import hashlib
import json
import os
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from weight_mcp.config import Settings
from weight_mcp.server import create_app

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
ADMIN = "admin"
PASSWORD = "secret"  # matches the `settings` fixture in conftest
RESOURCE = "https://weight.example.com/"  # MCP is served at the origin root


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def _initialize_body() -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), follow_redirects=False) as test_client:
        yield test_client


def _register(client: TestClient) -> str:
    resp = client.post(
        "/register",
        json={"redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"},
    )
    assert resp.status_code == 201
    client_id: str = resp.json()["client_id"]
    assert client_id
    return client_id


def _authorize_to_txn(client: TestClient, client_id: str, challenge: str) -> str:
    resp = client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st123",
            "resource": RESOURCE,
            "scope": "user",
        },
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("/login")
    return str(parse_qs(urlparse(location).query)["txn"][0])


def _obtain_token(client: TestClient, username: str, password: str) -> str:
    """Run the whole DCR + PKCE + login dance and return an access token."""
    verifier, challenge = _pkce()
    client_id = _register(client)
    txn = _authorize_to_txn(client, client_id, challenge)
    ok = client.post("/login", data={"txn": txn, "username": username, "password": password})
    assert ok.status_code == 302, "login should succeed"
    code = parse_qs(urlparse(ok.headers["location"]).query)["code"][0]
    token_resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert token_resp.status_code == 200
    token: str = token_resp.json()["access_token"]
    return token


class McpSession:
    """A minimal Streamable-HTTP MCP client: initialize once, then call tools."""

    def __init__(self, http: TestClient, token: str) -> None:
        self.http = http
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        resp = http.post("/", headers=self.headers, json=_initialize_body())
        assert resp.status_code == 200, resp.text
        self.headers["mcp-session-id"] = resp.headers["mcp-session-id"]
        http.post(
            "/",
            headers=self.headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        self._id = 1

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self._id += 1
        resp = self.http.post(
            "/",
            headers=self.headers,
            json={
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        assert resp.status_code == 200, resp.text
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                payload = json.loads(line.split("data:", 1)[1])
                if payload.get("id") == self._id:
                    result: dict[str, Any] = payload["result"]
                    return result
        raise AssertionError(f"no response for request {self._id}: {resp.text}")


def _text(result: dict[str, Any]) -> str:
    return "".join(c.get("text", "") for c in result.get("content", []))


def test_metadata_and_challenge(client: TestClient, settings: Settings) -> None:
    asm = client.get("/.well-known/oauth-authorization-server")
    assert asm.status_code == 200
    assert "S256" in asm.json()["code_challenge_methods_supported"]

    prm = client.get("/.well-known/oauth-protected-resource")
    assert prm.status_code == 200
    assert prm.json()["resource"] == f"{settings.issuer}/"

    unauth = client.get("/")
    assert unauth.status_code == 401
    assert "resource_metadata=" in unauth.headers["www-authenticate"]


def test_full_flow_login_gate_and_authenticated_call(client: TestClient) -> None:
    verifier, challenge = _pkce()
    client_id = _register(client)
    txn = _authorize_to_txn(client, client_id, challenge)

    # Wrong credentials are rejected, the transaction survives for a retry.
    bad = client.post("/login", data={"txn": txn, "username": ADMIN, "password": "nope"})
    assert bad.status_code == 401
    bad_user = client.post("/login", data={"txn": txn, "username": "ghost", "password": PASSWORD})
    assert bad_user.status_code == 401

    # Correct credentials redirect back to claude.ai with code + original state.
    ok = client.post("/login", data={"txn": txn, "username": ADMIN, "password": PASSWORD})
    assert ok.status_code == 302
    redirect = ok.headers["location"]
    assert redirect.startswith(REDIRECT)
    query = parse_qs(urlparse(redirect).query)
    assert query["state"][0] == "st123"
    code = query["code"][0]

    # Token exchange requires the matching PKCE verifier.
    token_resp = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert token_resp.status_code == 200
    body = token_resp.json()
    access_token = body["access_token"]
    assert access_token and body["refresh_token"]

    # The access token authenticates an MCP request.
    authed = client.post(
        "/",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json=_initialize_body(),
    )
    assert authed.status_code != 401


def test_login_works_across_instances(client: TestClient, settings: Settings) -> None:
    # The flow is stateless: a txn minted by one instance must complete on a
    # *different* one (multiple uvicorn workers, replicas, or a restart mid-flow).
    _, challenge = _pkce()
    client_id = _register(client)
    txn = _authorize_to_txn(client, client_id, challenge)

    with TestClient(create_app(settings), follow_redirects=False) as other:
        page = other.get(f"/login?txn={txn}")
        assert page.status_code == 200
        assert "expired" not in page.text.lower()
        done = other.post("/login", data={"txn": txn, "username": ADMIN, "password": PASSWORD})
        assert done.status_code == 302
        assert done.headers["location"].startswith(REDIRECT)


def test_tampered_txn_rejected(client: TestClient) -> None:
    bad = client.get("/login?txn=not-a-real-jwt")
    assert bad.status_code == 400
    assert "expired" in bad.text.lower()


def test_dashboard_cookie_gate(client: TestClient, settings: Settings) -> None:
    from weight_mcp.db import Database
    from weight_mcp.oauth import PasswordOAuthProvider
    from weight_mcp.server import DASHBOARD_COOKIE

    # No cookie → the login form (a stable URL), not the dashboard.
    form = client.get("/dashboard")
    assert form.status_code == 200
    assert "password" in form.text.lower()
    assert "Today" not in form.text

    # Wrong credentials are rejected.
    bad = client.post("/dashboard", data={"username": ADMIN, "password": "nope"})
    assert bad.status_code == 401

    # Correct credentials set a cookie and redirect.
    ok = client.post("/dashboard", data={"username": ADMIN, "password": PASSWORD})
    assert ok.status_code == 302
    assert DASHBOARD_COOKIE in ok.headers.get("set-cookie", "")

    # A valid cookie renders the dashboard. Cookies are signed with the
    # admin-password-derived key, so a provider from the same settings mints one
    # the app accepts; inject it directly (TestClient won't resend a Secure
    # cookie over http).
    provider = PasswordOAuthProvider(
        admin_password=settings.password,
        resource_url=RESOURCE,
        login_path="/login",
        db=Database(settings.database_path),
    )
    page = client.get(
        "/dashboard",
        headers={"Cookie": f"{DASHBOARD_COOKIE}={provider.dashboard_cookie(ADMIN)}"},
    )
    assert page.status_code == 200
    assert "Today" in page.text


def test_garbage_token_is_rejected(client: TestClient) -> None:
    resp = client.post(
        "/",
        headers={
            "Authorization": "Bearer garbage",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json=_initialize_body(),
    )
    assert resp.status_code == 401


def test_admin_manages_users_and_data_is_isolated(client: TestClient) -> None:
    admin = McpSession(client, _obtain_token(client, ADMIN, PASSWORD))

    # Admin registers alice; she can then complete the OAuth flow herself.
    result = admin.call_tool("register_user", {"username": "alice", "password": "alice-pw-123"})
    assert not result.get("isError"), _text(result)
    alice_token = _obtain_token(client, "alice", "alice-pw-123")
    alice = McpSession(client, alice_token)

    # Alice's data stays hers: her meal #1 does not appear for the admin.
    result = alice.call_tool(
        "log_food", {"name": "oats", "kcal": 300, "protein_g": 10, "meal_number": 1}
    )
    assert not result.get("isError"), _text(result)
    assert "oats" in _text(alice.call_tool("list_meals", {}))
    assert "oats" not in _text(admin.call_tool("list_meals", {}))

    # Non-admin accounts cannot manage users.
    denied = alice.call_tool("register_user", {"username": "bob", "password": "bob-pw-12345"})
    assert denied.get("isError")
    assert "admin" in _text(denied).lower()

    # A password update invalidates alice's outstanding token...
    result = admin.call_tool(
        "update_user_password", {"username": "alice", "password": "alice-pw-456"}
    )
    assert not result.get("isError"), _text(result)
    stale = client.post(
        "/",
        headers={
            "Authorization": f"Bearer {alice_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json=_initialize_body(),
    )
    assert stale.status_code == 401
    # ...and the new password works, with her data still there.
    alice2 = McpSession(client, _obtain_token(client, "alice", "alice-pw-456"))
    assert "oats" in _text(alice2.call_tool("list_meals", {}))

    # Deregistration revokes access outright: even a fresh login must fail.
    result = admin.call_tool("deregister_user", {"username": "alice"})
    assert not result.get("isError"), _text(result)
    _, challenge = _pkce()
    client_id = _register(client)
    txn = _authorize_to_txn(client, client_id, challenge)
    gone = client.post("/login", data={"txn": txn, "username": "alice", "password": "alice-pw-456"})
    assert gone.status_code == 401

    # Guardrails: bad usernames, short passwords, the reserved admin name.
    for args in (
        {"username": "b@d!", "password": "long-enough-pw"},
        {"username": "bob", "password": "short"},
        {"username": ADMIN, "password": "long-enough-pw"},
    ):
        result = admin.call_tool("register_user", args)
        assert result.get("isError"), args
