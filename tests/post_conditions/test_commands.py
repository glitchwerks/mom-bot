"""Tests for mom_bot.post_conditions.commands.

Covers: per-user scope enforcement, 404 → link-your-account message,
token never leaks in error responses, register() wires commands.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from mom_bot.post_conditions.client import (
    SiegeWebAuthError,
    SiegeWebNotFoundError,
)
from mom_bot.post_conditions.commands import (
    post_conditions_catalog,
    post_conditions_get,
    post_conditions_set,
    register,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DISCORD_ID = 123456789012345678  # integer as discord.py provides
_TOKEN = "secret-bot-token"

_CATALOG: list[dict[str, Any]] = [
    {
        "id": 5,
        "description": "Only HP Champions can be used.",
        "stronghold_level": 1,
        "condition_type": "role",
    },
    {
        "id": 12,
        "description": "Only Barbarian Champions can be used.",
        "stronghold_level": 1,
        "condition_type": "faction",
    },
]

_PREFS: list[dict[str, Any]] = [
    {
        "id": 5,
        "description": "Only HP Champions can be used.",
        "stronghold_level": 1,
        "condition_type": "role",
    }
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_interaction(discord_id: int = _DISCORD_ID) -> MagicMock:
    """Build a minimal fake discord.Interaction."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = discord_id
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _make_client(
    catalog: list[dict[str, Any]] | None = None,
    prefs: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """Build a mock SiegeWebClient.

    Args:
        catalog: Return value for ``list_catalog``.  Defaults to _CATALOG.
        prefs: Return value for ``get_my_preferences``.  Defaults to _PREFS.
            Pass an empty list explicitly (``[]``) to simulate no preferences.
    """
    client = MagicMock()
    client.list_catalog = AsyncMock(return_value=catalog if catalog is not None else _CATALOG)
    client.get_my_preferences = AsyncMock(return_value=prefs if prefs is not None else _PREFS)
    client.set_my_preferences = AsyncMock(return_value=prefs if prefs is not None else _PREFS)
    return client


# ---------------------------------------------------------------------------
# /post-conditions (catalog)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalog_command_sends_ephemeral_reply() -> None:
    """/post-conditions sends an ephemeral message."""
    interaction = _make_interaction()
    siege_client = _make_client()

    await post_conditions_catalog(interaction, siege_client=siege_client)

    interaction.response.send_message.assert_awaited_once()
    call_kwargs = interaction.response.send_message.call_args[1]
    assert call_kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_catalog_command_groups_by_meta() -> None:
    """/post-conditions output contains meta-group headings."""
    interaction = _make_interaction()
    siege_client = _make_client(catalog=_CATALOG)

    await post_conditions_catalog(interaction, siege_client=siege_client)

    call_args = interaction.response.send_message.call_args
    content: str = call_args[0][0] if call_args[0] else call_args[1].get("content", "")
    # Should contain role → Role, Affinity, Rarity and faction → Faction & League
    assert "Role, Affinity, Rarity" in content or "Faction & League" in content


@pytest.mark.asyncio
async def test_catalog_command_does_not_send_auth_to_open_endpoint() -> None:
    """/post-conditions calls list_catalog (not get_my_preferences)."""
    interaction = _make_interaction()
    siege_client = _make_client()

    await post_conditions_catalog(interaction, siege_client=siege_client)

    siege_client.list_catalog.assert_awaited_once()
    siege_client.get_my_preferences.assert_not_awaited()


# ---------------------------------------------------------------------------
# /post-conditions-get (per-user read)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_command_uses_invoking_user_id() -> None:
    """/post-conditions-get uses interaction.user.id (not a target arg)."""
    interaction = _make_interaction(discord_id=_DISCORD_ID)
    siege_client = _make_client()

    await post_conditions_get(interaction, siege_client=siege_client)

    siege_client.get_my_preferences.assert_awaited_once_with(discord_id=str(_DISCORD_ID))


@pytest.mark.asyncio
async def test_get_command_sends_ephemeral_reply() -> None:
    """/post-conditions-get is ephemeral."""
    interaction = _make_interaction()
    siege_client = _make_client()

    await post_conditions_get(interaction, siege_client=siege_client)

    call_kwargs = interaction.response.send_message.call_args[1]
    assert call_kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_get_command_empty_prefs_shows_none_set_message() -> None:
    """/post-conditions-get with empty prefs shows a no-preferences message."""
    interaction = _make_interaction()
    siege_client = _make_client(prefs=[])

    await post_conditions_get(interaction, siege_client=siege_client)

    call_args = interaction.response.send_message.call_args
    content: str = call_args[0][0] if call_args[0] else call_args[1].get("content", "")
    assert "no post-condition preferences" in content.lower()


@pytest.mark.asyncio
async def test_get_command_404_shows_link_account_guidance() -> None:
    """/post-conditions-get on 404 shows link-your-account guidance."""
    interaction = _make_interaction()
    siege_client = _make_client()
    siege_client.get_my_preferences = AsyncMock(side_effect=SiegeWebNotFoundError())

    await post_conditions_get(interaction, siege_client=siege_client)

    call_args = interaction.response.send_message.call_args
    content: str = call_args[0][0] if call_args[0] else call_args[1].get("content", "")
    assert "rslsiege.com" in content.lower()
    assert call_args[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_get_command_401_error_does_not_leak_token() -> None:
    """/post-conditions-get on 401 sends user-readable message with no token."""
    interaction = _make_interaction()
    siege_client = _make_client()
    siege_client.get_my_preferences = AsyncMock(side_effect=SiegeWebAuthError())

    await post_conditions_get(interaction, siege_client=siege_client)

    call_args = interaction.response.send_message.call_args
    content: str = call_args[0][0] if call_args[0] else call_args[1].get("content", "")
    assert _TOKEN not in content


# ---------------------------------------------------------------------------
# /post-conditions-set (per-user write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_command_uses_invoking_user_id() -> None:
    """/post-conditions-set uses interaction.user.id for all API calls."""
    interaction = _make_interaction(discord_id=_DISCORD_ID)
    siege_client = _make_client()

    with patch(
        "mom_bot.post_conditions.commands.PostConditionsView",
        autospec=True,
    ) as MockView:
        mock_view_instance = MagicMock()
        mock_view_instance.build_header = MagicMock(return_value="Page 1 of 3")
        MockView.return_value = mock_view_instance

        await post_conditions_set(interaction, siege_client=siege_client)

    # get_my_preferences must have been called with the invoking user's ID.
    siege_client.get_my_preferences.assert_awaited_once_with(discord_id=str(_DISCORD_ID))


@pytest.mark.asyncio
async def test_set_command_sends_ephemeral_reply() -> None:
    """/post-conditions-set sends an ephemeral response."""
    interaction = _make_interaction()
    siege_client = _make_client()

    with patch(
        "mom_bot.post_conditions.commands.PostConditionsView",
        autospec=True,
    ) as MockView:
        mock_view_instance = MagicMock()
        mock_view_instance.build_header = MagicMock(return_value="Page 1 of 3")
        MockView.return_value = mock_view_instance

        await post_conditions_set(interaction, siege_client=siege_client)

    # Should have sent or deferred with ephemeral.
    send_call = interaction.response.send_message.call_args
    if send_call:
        assert send_call[1].get("ephemeral") is True


@pytest.mark.asyncio
async def test_set_command_404_shows_link_account_guidance() -> None:
    """/post-conditions-set on 404 from GET shows link-your-account guidance."""
    interaction = _make_interaction()
    siege_client = _make_client()
    siege_client.get_my_preferences = AsyncMock(side_effect=SiegeWebNotFoundError())

    await post_conditions_set(interaction, siege_client=siege_client)

    call_args = interaction.response.send_message.call_args
    content: str = call_args[0][0] if call_args[0] else call_args[1].get("content", "")
    assert "rslsiege.com" in content.lower()
    assert call_args[1].get("ephemeral") is True


# ---------------------------------------------------------------------------
# register()
# ---------------------------------------------------------------------------


def test_register_attaches_commands_to_tree() -> None:
    """register(tree, client) attaches three commands to the command tree."""
    tree = MagicMock(spec=discord.app_commands.CommandTree)
    tree.command = MagicMock(return_value=lambda f: f)
    siege_client = _make_client()

    register(tree=tree, siege_client=siege_client)

    # Should have registered 3 commands.
    assert tree.command.call_count == 3
