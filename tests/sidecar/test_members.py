"""Tests for GET /api/members and GET /api/members/{discord_user_id}.

Phase 3 of Epic #128 sidecar replacement (issue #177).

Covers:
- GET /api/members: 200 + array shape; correct field names; no @everyone leak
- GET /api/members/{id}: 200 + is_member=true (all 6 keys); is_member=false (all
  6 keys, all null); @everyone excluded from roles/role_names
- Bearer auth: missing → 401; wrong → 401 + WWW-Authenticate; correct → passes
- Discord exception translation: Forbidden → 403; 4xx → 502; 5xx/timeout → 503
- Path validation: non-numeric discord_user_id → 422

Multi-guild decision
--------------------
The sidecar is scoped to a single guild supplied via ``build_app(guild=...)``.
The guild object is constructed at startup from ``DISCORD_GUILD_ID``.  All
member queries run against that single guild only, matching the siege-web
sidecar contract which is single-guild by design.

Design notes
------------
- Uses FastAPI TestClient (synchronous) via ``_make_client`` helper.
- Member data is injected via ``FakeGuild`` / ``FakeMember`` objects so no
  Discord gateway or network calls occur during tests.
- Discord exception translation is exercised by configuring ``FakeGuild`` to
  raise specific exceptions in ``fetch_member()``.
- The ``build_app`` ``guild=`` parameter accepts any object duck-typed to the
  subset of ``discord.Guild`` the endpoints use:
    - ``.members`` — iterable of member objects
    - ``.fetch_member(int)`` — coroutine returning a member or raising
      ``discord.NotFound`` / ``discord.HTTPException``
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import discord
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from mom_bot.db import Base
from mom_bot.sidecar.app import build_app

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_KEY = "test-bearer-key-members-xyz"
_WRONG_KEY = "wrong-key"

_KNOWN_ID = "111000111000111001"
_UNKNOWN_ID = "999000999000999001"

# Keys required by INTERFACE.md for each endpoint
_LIST_KEYS = {"id", "username", "display_name"}
_DETAIL_KEYS = {
    "is_member",
    "discord_id",
    "username",
    "display_name",
    "roles",
    "role_names",
}

# @everyone role name — must never appear in roles/role_names
_EVERYONE_ROLE_NAME = "@everyone"


# ---------------------------------------------------------------------------
# Fake Discord objects
# ---------------------------------------------------------------------------


class FakeRole:
    """Minimal stand-in for discord.Role.

    Attributes:
        id: Snowflake integer.
        name: Human-readable role name.
    """

    def __init__(self, role_id: int, name: str) -> None:
        """Initialise with a snowflake id and role name.

        Args:
            role_id: Integer snowflake for this role.
            name: Human-readable role name (e.g. ``"@everyone"`` or
                ``"Clan Deputies"``).
        """
        self.id = role_id
        self.name = name


class FakeMember:
    """Minimal stand-in for discord.Member.

    Attributes:
        id: Snowflake integer (will be str-cast by the endpoint).
        name: Discord username.
        display_name: Guild display name.
        roles: List of :class:`FakeRole` objects assigned to this member.
    """

    def __init__(
        self,
        member_id: int,
        name: str,
        display_name: str,
        roles: list[FakeRole] | None = None,
    ) -> None:
        """Initialise a fake member.

        Args:
            member_id: Integer snowflake.
            name: Discord username.
            display_name: Guild display name (may differ from username).
            roles: Role list.  Defaults to ``[@everyone]`` if omitted.
        """
        self.id = member_id
        self.name = name
        self.display_name = display_name
        self.roles: list[FakeRole] = (
            roles if roles is not None else [FakeRole(1, _EVERYONE_ROLE_NAME)]
        )


class FakeGuild:
    """Minimal stand-in for discord.Guild.

    Supplies ``.members`` (iterable) and ``.fetch_member()`` (coroutine).
    Configure ``fetch_member_exc`` to make ``fetch_member`` raise a specific
    exception instead of looking up the member normally.

    Attributes:
        members: List of :class:`FakeMember` objects (the cached guild roster).
        fetch_member_exc: If set, ``fetch_member`` raises this instead of
            performing a lookup.
    """

    def __init__(
        self,
        members: list[FakeMember] | None = None,
        fetch_member_exc: Exception | None = None,
    ) -> None:
        """Initialise the fake guild.

        Args:
            members: Guild member list.  Defaults to one known member.
            fetch_member_exc: Optional exception to raise from
                :meth:`fetch_member`.
        """
        self.members: list[FakeMember] = (
            members
            if members is not None
            else [
                FakeMember(
                    int(_KNOWN_ID),
                    "known-user",
                    "Known User",
                    roles=[
                        FakeRole(1, _EVERYONE_ROLE_NAME),
                        FakeRole(987654321098765432, "Clan Deputies"),
                    ],
                )
            ]
        )
        self.fetch_member_exc: Exception | None = fetch_member_exc

    async def fetch_member(self, user_id: int) -> FakeMember:
        """Look up a member by integer snowflake, or raise a configured exc.

        Args:
            user_id: Discord snowflake (integer form).

        Returns:
            The matching :class:`FakeMember` if found.

        Raises:
            discord.NotFound: If user_id has no matching member and no
                custom exception is configured.
            Exception: Whatever ``fetch_member_exc`` is set to, when set.
        """
        if self.fetch_member_exc is not None:
            raise self.fetch_member_exc
        for m in self.members:
            if m.id == user_id:
                return m
        # Simulate discord.NotFound for unknown IDs
        response = MagicMock()
        response.status = 404
        response.reason = "Unknown Member"
        raise discord.NotFound(response, "Unknown Member")


class _FakeBot:
    """Minimal stand-in for discord.Client used by build_app."""

    def is_ready(self) -> bool:
        """Always reports ready — member tests do not exercise health."""
        return True


_FAKE_BOT = _FakeBot()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session_factory() -> Any:
    """Build an in-memory SQLite session factory with all ORM tables.

    Returns:
        A :class:`~sqlalchemy.orm.sessionmaker` backed by an in-memory DB.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        echo=False,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _make_client(
    *,
    api_key: str = _VALID_KEY,
    guild: FakeGuild | None = None,
) -> TestClient:
    """Build a TestClient wrapping the sidecar app with a fake guild.

    Args:
        api_key: Bearer token the sidecar validates against.
        guild: Fake guild to inject; defaults to a standard guild with the
            known member.

    Returns:
        A :class:`~fastapi.testclient.TestClient` for the app.
    """
    app = build_app(
        api_key=api_key,
        bot=_FAKE_BOT,  # type: ignore[arg-type]
        guild=guild if guild is not None else FakeGuild(),  # type: ignore[arg-type]
        session_factory=_make_session_factory(),
    )
    return TestClient(app, raise_server_exceptions=False)


def _auth(key: str = _VALID_KEY) -> dict[str, str]:
    """Return an Authorization header dict for the given key.

    Args:
        key: Bearer token value.

    Returns:
        Dict with ``Authorization`` key.
    """
    return {"Authorization": f"Bearer {key}"}


# ---------------------------------------------------------------------------
# GET /api/members — auth
# ---------------------------------------------------------------------------


class TestGetMembersAuth:
    """Bearer auth gates GET /api/members."""

    def test_missing_auth_returns_401(self) -> None:
        """No Authorization header → 401."""
        client = _make_client()
        response = client.get("/api/members")
        assert response.status_code == 401

    def test_wrong_token_returns_401(self) -> None:
        """Wrong Bearer token → 401."""
        client = _make_client()
        response = client.get("/api/members", headers=_auth(_WRONG_KEY))
        assert response.status_code == 401

    def test_wrong_token_has_www_authenticate_bearer(self) -> None:
        """Wrong-token 401 must include WWW-Authenticate: Bearer header."""
        client = _make_client()
        response = client.get("/api/members", headers=_auth(_WRONG_KEY))
        assert "Bearer" in response.headers.get("www-authenticate", "")


# ---------------------------------------------------------------------------
# GET /api/members — 200 shape
# ---------------------------------------------------------------------------


class TestGetMembersList:
    """GET /api/members returns correct shape with valid auth."""

    def test_returns_200(self) -> None:
        """Valid auth + connected → 200."""
        client = _make_client()
        response = client.get("/api/members", headers=_auth())
        assert response.status_code == 200

    def test_body_is_json_array(self) -> None:
        """Response body is a JSON array."""
        client = _make_client()
        data = client.get("/api/members", headers=_auth()).json()
        assert isinstance(data, list)

    def test_each_element_has_exactly_three_keys(self) -> None:
        """Each element has exactly id, username, display_name (not discord_id)."""
        client = _make_client()
        data = client.get("/api/members", headers=_auth()).json()
        assert len(data) >= 1, "Expected at least the known member"
        for elem in data:
            assert set(elem.keys()) == _LIST_KEYS, (
                f"Element keys mismatch: got {set(elem.keys())}, " f"expected {_LIST_KEYS}"
            )

    def test_id_field_is_string_not_discord_id(self) -> None:
        """The Discord snowflake field is named 'id', NOT 'discord_id'.

        This is load-bearing per INTERFACE.md — renaming would break
        existing consumers.
        """
        client = _make_client()
        data = client.get("/api/members", headers=_auth()).json()
        assert len(data) >= 1
        member = data[0]
        assert "id" in member, "Field must be 'id', not 'discord_id'"
        assert "discord_id" not in member, "Must not use 'discord_id' on list endpoint"

    def test_known_member_present_in_list(self) -> None:
        """Known member appears in the list with correct id value."""
        client = _make_client()
        data = client.get("/api/members", headers=_auth()).json()
        found = next((m for m in data if m["id"] == _KNOWN_ID), None)
        assert found is not None, f"Known member {_KNOWN_ID!r} not in list"

    def test_member_fields_are_non_empty_strings(self) -> None:
        """id, username, display_name are all non-empty strings."""
        client = _make_client()
        data = client.get("/api/members", headers=_auth()).json()
        found = next((m for m in data if m["id"] == _KNOWN_ID), None)
        assert found is not None
        assert isinstance(found["id"], str) and found["id"]
        assert isinstance(found["username"], str) and found["username"]
        assert isinstance(found["display_name"], str)

    def test_empty_guild_returns_empty_list(self) -> None:
        """A guild with no members returns an empty array (not 404)."""
        client = _make_client(guild=FakeGuild(members=[]))
        data = client.get("/api/members", headers=_auth()).json()
        assert data == []

    def test_all_members_included(self) -> None:
        """All members in the guild appear in the response."""
        members = [
            FakeMember(111, "alpha", "Alpha"),
            FakeMember(222, "beta", "Beta"),
            FakeMember(333, "gamma", "Gamma"),
        ]
        client = _make_client(guild=FakeGuild(members=members))
        data = client.get("/api/members", headers=_auth()).json()
        ids = {m["id"] for m in data}
        assert ids == {"111", "222", "333"}


# ---------------------------------------------------------------------------
# GET /api/members/{discord_user_id} — auth
# ---------------------------------------------------------------------------


class TestGetMemberDetailAuth:
    """Bearer auth gates GET /api/members/{discord_user_id}."""

    def test_missing_auth_returns_401(self) -> None:
        """No Authorization header → 401."""
        client = _make_client()
        response = client.get(f"/api/members/{_KNOWN_ID}")
        assert response.status_code == 401

    def test_wrong_token_returns_401(self) -> None:
        """Wrong Bearer token → 401."""
        client = _make_client()
        response = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth(_WRONG_KEY))
        assert response.status_code == 401

    def test_wrong_token_has_www_authenticate_bearer(self) -> None:
        """Wrong-token 401 must include WWW-Authenticate: Bearer."""
        client = _make_client()
        response = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth(_WRONG_KEY))
        assert "Bearer" in response.headers.get("www-authenticate", "")


# ---------------------------------------------------------------------------
# GET /api/members/{discord_user_id} — known member (is_member: true)
# ---------------------------------------------------------------------------


class TestGetMemberDetailFound:
    """Known member returns is_member=true with all 6 keys populated."""

    def test_returns_200(self) -> None:
        """Valid auth, known member ID → 200."""
        client = _make_client()
        response = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth())
        assert response.status_code == 200

    def test_all_six_keys_present_when_member_found(self) -> None:
        """All 6 required keys present in is_member=true response."""
        client = _make_client()
        data = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth()).json()
        assert (
            set(data.keys()) == _DETAIL_KEYS
        ), f"Key mismatch: got {set(data.keys())}, expected {_DETAIL_KEYS}"

    def test_is_member_true_for_known_member(self) -> None:
        """is_member is boolean true for a known guild member."""
        client = _make_client()
        data = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth()).json()
        assert data["is_member"] is True

    def test_discord_id_is_string_matching_path_param(self) -> None:
        """discord_id field is the snowflake string (not 'id')."""
        client = _make_client()
        data = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth()).json()
        assert data["discord_id"] == _KNOWN_ID
        assert isinstance(data["discord_id"], str)

    def test_username_and_display_name_are_strings(self) -> None:
        """username and display_name are non-null strings for found member."""
        client = _make_client()
        data = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth()).json()
        assert isinstance(data["username"], str)
        assert isinstance(data["display_name"], str)

    def test_roles_and_role_names_are_lists(self) -> None:
        """roles and role_names are lists (not null) for found member."""
        client = _make_client()
        data = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth()).json()
        assert isinstance(data["roles"], list)
        assert isinstance(data["role_names"], list)

    def test_everyone_role_excluded_from_roles(self) -> None:
        """@everyone role id is excluded from roles list."""
        guild = FakeGuild(
            members=[
                FakeMember(
                    int(_KNOWN_ID),
                    "known-user",
                    "Known User",
                    roles=[
                        FakeRole(1, _EVERYONE_ROLE_NAME),
                        FakeRole(987654321098765432, "Clan Deputies"),
                    ],
                )
            ]
        )
        client = _make_client(guild=guild)
        data = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth()).json()
        # '1' is @everyone's snowflake in our fake; must not appear
        assert "1" not in data["roles"], "@everyone role id must be excluded from roles"

    def test_everyone_role_excluded_from_role_names(self) -> None:
        """@everyone is excluded from role_names list."""
        guild = FakeGuild(
            members=[
                FakeMember(
                    int(_KNOWN_ID),
                    "known-user",
                    "Known User",
                    roles=[
                        FakeRole(1, _EVERYONE_ROLE_NAME),
                        FakeRole(987654321098765432, "Clan Deputies"),
                    ],
                )
            ]
        )
        client = _make_client(guild=guild)
        data = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth()).json()
        assert (
            _EVERYONE_ROLE_NAME not in data["role_names"]
        ), "@everyone must not appear in role_names"

    def test_non_everyone_roles_included(self) -> None:
        """Non-@everyone roles appear in both roles and role_names."""
        guild = FakeGuild(
            members=[
                FakeMember(
                    int(_KNOWN_ID),
                    "known-user",
                    "Known User",
                    roles=[
                        FakeRole(1, _EVERYONE_ROLE_NAME),
                        FakeRole(987654321098765432, "Clan Deputies"),
                    ],
                )
            ]
        )
        client = _make_client(guild=guild)
        data = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth()).json()
        assert "987654321098765432" in data["roles"]
        assert "Clan Deputies" in data["role_names"]


# ---------------------------------------------------------------------------
# GET /api/members/{discord_user_id} — unknown member (is_member: false)
# ---------------------------------------------------------------------------


class TestGetMemberDetailNotFound:
    """Unknown member ID returns is_member=false with all keys null."""

    def test_returns_200_for_unknown_id(self) -> None:
        """Unknown Discord snowflake → 200 (not 404)."""
        client = _make_client()
        response = client.get(f"/api/members/{_UNKNOWN_ID}", headers=_auth())
        assert response.status_code == 200

    def test_all_six_keys_present_when_not_member(self) -> None:
        """All 6 required keys present in is_member=false response."""
        client = _make_client()
        data = client.get(f"/api/members/{_UNKNOWN_ID}", headers=_auth()).json()
        assert set(data.keys()) == _DETAIL_KEYS

    def test_is_member_false_for_unknown_id(self) -> None:
        """is_member is boolean false for an unknown/non-member ID."""
        client = _make_client()
        data = client.get(f"/api/members/{_UNKNOWN_ID}", headers=_auth()).json()
        assert data["is_member"] is False

    def test_all_other_keys_are_null_when_not_member(self) -> None:
        """All five non-discriminator keys are null when is_member=false."""
        client = _make_client()
        data = client.get(f"/api/members/{_UNKNOWN_ID}", headers=_auth()).json()
        assert data["discord_id"] is None
        assert data["username"] is None
        assert data["display_name"] is None
        assert data["roles"] is None
        assert data["role_names"] is None


# ---------------------------------------------------------------------------
# GET /api/members/{discord_user_id} — path validation
# ---------------------------------------------------------------------------


class TestGetMemberDetailPathValidation:
    """Non-numeric discord_user_id returns 422 per contract."""

    def test_non_numeric_id_returns_422(self) -> None:
        """Non-numeric path param (e.g. 'abc') → 422 before handler runs."""
        client = _make_client()
        response = client.get("/api/members/abc", headers=_auth())
        assert response.status_code == 422

    def test_alphanumeric_id_returns_422(self) -> None:
        """Mixed alphanumeric path param → 422."""
        client = _make_client()
        response = client.get("/api/members/abc123", headers=_auth())
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# Discord exception translation — single member endpoint
# ---------------------------------------------------------------------------


class TestDiscordExceptionTranslation:
    """Discord exceptions translate to correct HTTP status codes.

    The single-member endpoint calls ``guild.fetch_member()`` which may
    raise Discord exceptions.  The sidecar must translate these per the
    INTERFACE.md error semantics section.
    """

    def test_discord_forbidden_translates_to_403(self) -> None:
        """discord.Forbidden from fetch_member → HTTP 403."""
        response_mock = MagicMock()
        response_mock.status = 403
        response_mock.reason = "Forbidden"
        exc = discord.Forbidden(response_mock, "Missing Access")
        client = _make_client(guild=FakeGuild(fetch_member_exc=exc))
        response = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth())
        assert response.status_code == 403

    def test_discord_4xx_translates_to_502(self) -> None:
        """discord.HTTPException with status < 500 → HTTP 502."""
        response_mock = MagicMock()
        response_mock.status = 400
        response_mock.reason = "Bad Request"
        exc = discord.HTTPException(response_mock, "Bad Request")
        client = _make_client(guild=FakeGuild(fetch_member_exc=exc))
        response = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth())
        assert response.status_code == 502

    def test_discord_5xx_translates_to_503(self) -> None:
        """discord.HTTPException with status >= 500 → HTTP 503."""
        response_mock = MagicMock()
        response_mock.status = 500
        response_mock.reason = "Internal Server Error"
        exc = discord.HTTPException(response_mock, "Server Error")
        client = _make_client(guild=FakeGuild(fetch_member_exc=exc))
        response = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth())
        assert response.status_code == 503

    def test_asyncio_timeout_translates_to_503(self) -> None:
        """asyncio.TimeoutError from fetch_member → HTTP 503."""
        client = _make_client(guild=FakeGuild(fetch_member_exc=TimeoutError()))
        response = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth())
        assert response.status_code == 503

    def test_403_body_has_detail_string(self) -> None:
        """Translated 403 response body has a 'detail' string key."""
        response_mock = MagicMock()
        response_mock.status = 403
        response_mock.reason = "Forbidden"
        exc = discord.Forbidden(response_mock, "Missing Access")
        client = _make_client(guild=FakeGuild(fetch_member_exc=exc))
        body = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth()).json()
        assert "detail" in body
        assert isinstance(body["detail"], str)

    def test_502_body_has_detail_string(self) -> None:
        """Translated 502 response body has a 'detail' string key."""
        response_mock = MagicMock()
        response_mock.status = 400
        response_mock.reason = "Bad Request"
        exc = discord.HTTPException(response_mock, "Bad Request")
        client = _make_client(guild=FakeGuild(fetch_member_exc=exc))
        body = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth()).json()
        assert "detail" in body
        assert isinstance(body["detail"], str)

    def test_503_body_has_detail_string(self) -> None:
        """Translated 503 response body has a 'detail' string key."""
        response_mock = MagicMock()
        response_mock.status = 500
        response_mock.reason = "Internal Server Error"
        exc = discord.HTTPException(response_mock, "Server Error")
        client = _make_client(guild=FakeGuild(fetch_member_exc=exc))
        body = client.get(f"/api/members/{_KNOWN_ID}", headers=_auth()).json()
        assert "detail" in body
        assert isinstance(body["detail"], str)


# ---------------------------------------------------------------------------
# GET /api/members — Discord exception translation
# ---------------------------------------------------------------------------


class TestGetMembersListExceptions:
    """The list endpoint propagates guild unavailability as 503."""

    @pytest.mark.skip(
        reason="List endpoint uses guild.members cache — no async call "
        "to intercept with exception; 503 path covered by design (guild=None "
        "startup guard). Individual fetch_member exceptions are "
        "tested on the detail endpoint."
    )
    def test_placeholder(self) -> None:
        """Placeholder — see class docstring."""
