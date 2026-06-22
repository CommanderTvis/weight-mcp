import asyncio

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from weight_mcp.db import Database
from weight_mcp.oauth import SCOPE, PasswordOAuthProvider

RESOURCE = "https://weight.example.com/"


def make_provider(db: Database, password: str = "secret") -> PasswordOAuthProvider:
    return PasswordOAuthProvider(
        password=password, resource_url=RESOURCE, login_path="/login", db=db
    )


def test_password_check(db: Database) -> None:
    provider = make_provider(db)
    assert provider.password_ok("secret")
    assert not provider.password_ok("wrong")


async def test_access_token_roundtrip(db: Database) -> None:
    provider = make_provider(db)
    token = provider._issue("client-1")
    loaded = await provider.load_access_token(token.access_token)
    assert loaded is not None
    assert loaded.client_id == "client-1"
    assert loaded.resource == RESOURCE


async def test_token_rejected_after_password_change(db: Database) -> None:
    token = make_provider(db, "old")._issue("client-1")
    rotated = make_provider(db, "new")
    assert await rotated.load_access_token(token.access_token) is None


async def test_refresh_token_not_accepted_as_access(db: Database) -> None:
    provider = make_provider(db)
    token = provider._issue("client-1")
    assert token.refresh_token is not None
    assert await provider.load_access_token(token.refresh_token) is None


def test_dashboard_token_roundtrip(db: Database) -> None:
    provider = make_provider(db)
    token = provider.dashboard_link_token()
    assert provider.dashboard_token_valid(token)
    assert not provider.dashboard_token_valid("garbage")
    # An access token must not pass as a dashboard view token (typ mismatch).
    assert not provider.dashboard_token_valid(provider._issue("c1").access_token)
    # Rotating the password invalidates the link.
    assert not make_provider(db, "other").dashboard_token_valid(token)


def test_invalid_txn_rejected(db: Database) -> None:
    provider = make_provider(db)
    assert not provider.txn_valid("nope")
    assert provider.complete_login("nope") is None


def test_txn_from_one_password_invalid_after_rotation(db: Database) -> None:
    # A txn minted under the old password must not validate under a new one.
    old = make_provider(db, "old")
    params = AuthorizationParams(
        state="s",
        scopes=[SCOPE],
        code_challenge="abc",
        redirect_uri=AnyUrl("https://claude.ai/api/mcp/auth_callback"),
        redirect_uri_provided_explicitly=True,
        resource=RESOURCE,
    )
    client = OAuthClientInformationFull(
        client_id="c1", redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")]
    )
    login_url = asyncio.run(old.authorize(client, params))
    txn = login_url.split("txn=", 1)[1]
    assert old.txn_valid(txn)
    assert not make_provider(db, "new").txn_valid(txn)
