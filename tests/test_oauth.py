from __future__ import annotations

from weight_mcp.db import Database
from weight_mcp.oauth import PasswordOAuthProvider

RESOURCE = "https://weight.example.com/mcp"


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


def test_pending_and_login_unknown_txn(db: Database) -> None:
    provider = make_provider(db)
    assert not provider.pending_exists("nope")
    assert provider.complete_login("nope") is None
