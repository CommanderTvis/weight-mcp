import asyncio

from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl

from weight_mcp.db import Database, hash_password
from weight_mcp.oauth import ADMIN_USERNAME, SCOPE, PasswordOAuthProvider

RESOURCE = "https://weight.example.com/"


def make_provider(db: Database, password: str = "secret") -> PasswordOAuthProvider:
    return PasswordOAuthProvider(
        admin_password=password, resource_url=RESOURCE, login_path="/login", db=db
    )


def test_admin_login_check(db: Database) -> None:
    provider = make_provider(db)
    assert provider.verify_login(ADMIN_USERNAME, "secret")
    assert not provider.verify_login(ADMIN_USERNAME, "wrong")
    assert not provider.verify_login("nobody", "secret")


def test_db_user_login_check(db: Database) -> None:
    provider = make_provider(db)
    db.create_user("alice", hash_password("alice-pw"))
    assert provider.verify_login("alice", "alice-pw")
    assert not provider.verify_login("alice", "secret")  # admin pw doesn't unlock users
    assert not provider.verify_login(ADMIN_USERNAME, "alice-pw")


async def test_access_token_roundtrip(db: Database) -> None:
    provider = make_provider(db)
    token = provider._issue("client-1", ADMIN_USERNAME)
    loaded = await provider.load_access_token(token.access_token)
    assert loaded is not None
    assert loaded.client_id == "client-1"
    assert loaded.resource == RESOURCE
    assert loaded.subject == ADMIN_USERNAME


async def test_access_token_carries_db_user_subject(db: Database) -> None:
    provider = make_provider(db)
    db.create_user("alice", hash_password("alice-pw"))
    token = provider._issue("client-1", "alice")
    loaded = await provider.load_access_token(token.access_token)
    assert loaded is not None
    assert loaded.subject == "alice"


async def test_token_rejected_after_admin_password_change(db: Database) -> None:
    token = make_provider(db, "old")._issue("client-1", ADMIN_USERNAME)
    rotated = make_provider(db, "new")
    assert await rotated.load_access_token(token.access_token) is None


async def test_user_token_rejected_after_their_password_update(db: Database) -> None:
    provider = make_provider(db)
    db.create_user("alice", hash_password("alice-pw"))
    token = provider._issue("client-1", "alice")
    assert await provider.load_access_token(token.access_token) is not None

    db.set_user_password("alice", hash_password("new-pw"))
    assert await provider.load_access_token(token.access_token) is None
    # Admin tokens are unaffected by a user's password change.
    admin_token = provider._issue("client-1", ADMIN_USERNAME)
    assert await provider.load_access_token(admin_token.access_token) is not None


async def test_user_token_rejected_after_deregistration(db: Database) -> None:
    provider = make_provider(db)
    db.create_user("alice", hash_password("alice-pw"))
    token = provider._issue("client-1", "alice")
    assert await provider.load_access_token(token.access_token) is not None
    db.delete_user("alice")
    assert await provider.load_access_token(token.access_token) is None
    assert (
        await provider.load_refresh_token(
            OAuthClientInformationFull(
                client_id="client-1",
                redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")],
            ),
            token.refresh_token or "",
        )
        is None
    )


async def test_refresh_token_not_accepted_as_access(db: Database) -> None:
    provider = make_provider(db)
    token = provider._issue("client-1", ADMIN_USERNAME)
    assert token.refresh_token is not None
    assert await provider.load_access_token(token.refresh_token) is None


def test_dashboard_cookie_roundtrip(db: Database) -> None:
    provider = make_provider(db)
    db.create_user("alice", hash_password("alice-pw"))
    assert provider.dashboard_cookie_user(provider.dashboard_cookie(ADMIN_USERNAME)) == (
        ADMIN_USERNAME
    )
    assert provider.dashboard_cookie_user(provider.dashboard_cookie("alice")) == "alice"
    assert provider.dashboard_cookie_user("garbage") is None
    # An access token must not pass as a dashboard cookie (typ mismatch).
    access = provider._issue("c1", ADMIN_USERNAME).access_token
    assert provider.dashboard_cookie_user(access) is None
    # Rotating the admin password invalidates outstanding cookies.
    cookie = provider.dashboard_cookie(ADMIN_USERNAME)
    assert make_provider(db, "other").dashboard_cookie_user(cookie) is None
    # Deregistering a user invalidates their cookie.
    alice_cookie = provider.dashboard_cookie("alice")
    db.delete_user("alice")
    assert provider.dashboard_cookie_user(alice_cookie) is None


def test_invalid_txn_rejected(db: Database) -> None:
    provider = make_provider(db)
    assert not provider.txn_valid("nope")
    assert provider.complete_login("nope", ADMIN_USERNAME) is None


async def _authorize_txn(provider: PasswordOAuthProvider) -> str:
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
    login_url = await provider.authorize(client, params)
    return login_url.split("txn=", 1)[1]


def test_txn_from_one_password_invalid_after_rotation(db: Database) -> None:
    # A txn minted under the old admin password must not validate under a new one.
    old = make_provider(db, "old")
    txn = asyncio.run(_authorize_txn(old))
    assert old.txn_valid(txn)
    assert not make_provider(db, "new").txn_valid(txn)


async def test_auth_code_names_the_logged_in_user(db: Database) -> None:
    provider = make_provider(db)
    db.create_user("alice", hash_password("alice-pw"))
    client = OAuthClientInformationFull(
        client_id="c1", redirect_uris=[AnyUrl("https://claude.ai/api/mcp/auth_callback")]
    )
    txn = await _authorize_txn(provider)
    redirect = provider.complete_login(txn, "alice")
    assert redirect is not None
    code = redirect.split("code=", 1)[1].split("&", 1)[0]
    loaded = await provider.load_authorization_code(client, code)
    assert loaded is not None
    assert loaded.subject == "alice"
    # A txn (pre-login, no user) must not be accepted as a code.
    assert await provider.load_authorization_code(client, txn) is None
    # If the user is deregistered before the exchange, the code dies with them.
    db.delete_user("alice")
    assert await provider.load_authorization_code(client, code) is None
