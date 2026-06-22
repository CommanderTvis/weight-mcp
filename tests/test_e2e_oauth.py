"""End-to-end OAuth + MCP flow through the SDK's mounted routes (no network).

Exercises the whole connector handshake the way claude.ai would: Dynamic Client
Registration, the PKCE authorize redirect, the password gate, the token exchange,
and a finally-authenticated MCP request.
"""

import base64
import hashlib
import os
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from weight_mcp.config import Settings
from weight_mcp.server import create_app

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
PASSWORD = "secret"  # matches the `settings` fixture in conftest
RESOURCE = "https://weight.example.com/mcp"  # public_base_url from the fixture + /mcp


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


def test_metadata_and_challenge(client: TestClient, settings: Settings) -> None:
    asm = client.get("/.well-known/oauth-authorization-server")
    assert asm.status_code == 200
    assert "S256" in asm.json()["code_challenge_methods_supported"]

    prm = client.get("/.well-known/oauth-protected-resource/mcp")
    assert prm.status_code == 200
    assert prm.json()["resource"] == f"{settings.issuer}/mcp"

    unauth = client.get("/mcp")
    assert unauth.status_code == 401
    assert "resource_metadata=" in unauth.headers["www-authenticate"]


def test_full_flow_password_gate_and_authenticated_call(client: TestClient) -> None:
    verifier, challenge = _pkce()
    client_id = _register(client)
    txn = _authorize_to_txn(client, client_id, challenge)

    # Wrong password is rejected, the transaction survives for a retry.
    bad = client.post("/login", data={"txn": txn, "password": "nope"})
    assert bad.status_code == 401

    # Correct password redirects back to claude.ai with code + original state.
    ok = client.post("/login", data={"txn": txn, "password": PASSWORD})
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
        "/mcp",
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
        done = other.post("/login", data={"txn": txn, "password": PASSWORD})
        assert done.status_code == 302
        assert done.headers["location"].startswith(REDIRECT)


def test_tampered_txn_rejected(client: TestClient) -> None:
    bad = client.get("/login?txn=not-a-real-jwt")
    assert bad.status_code == 400
    assert "expired" in bad.text.lower()


def test_garbage_token_is_rejected(client: TestClient) -> None:
    resp = client.post(
        "/mcp",
        headers={
            "Authorization": "Bearer garbage",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json=_initialize_body(),
    )
    assert resp.status_code == 401
