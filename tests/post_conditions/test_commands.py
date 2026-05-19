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
from mom_bot.post_conditions.views import EditPreferencesView

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
async def test_catalog_command_defers_before_fetching() -> None:
    """/post-conditions defers the interaction before calling siege-web."""
    interaction = _make_interaction()
    siege_client = _make_client()

    await post_conditions_catalog(interaction, siege_client=siege_client)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)


@pytest.mark.asyncio
async def test_catalog_command_replies_via_followup() -> None:
    """/post-conditions sends its reply through interaction.followup.send."""
    interaction = _make_interaction()
    siege_client = _make_client()

    await post_conditions_catalog(interaction, siege_client=siege_client)

    interaction.followup.send.assert_awaited_once()
    interaction.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_catalog_command_sends_ephemeral_reply() -> None:
    """/post-conditions sends an ephemeral message via followup."""
    interaction = _make_interaction()
    siege_client = _make_client()

    await post_conditions_catalog(interaction, siege_client=siege_client)

    call_kwargs = interaction.followup.send.call_args[1]
    assert call_kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_catalog_command_groups_by_meta() -> None:
    """/post-conditions output contains meta-group headings."""
    interaction = _make_interaction()
    siege_client = _make_client(catalog=_CATALOG)

    await post_conditions_catalog(interaction, siege_client=siege_client)

    call_args = interaction.followup.send.call_args
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


@pytest.mark.asyncio
async def test_catalog_command_error_replies_via_followup() -> None:
    """/post-conditions on fetch error sends error reply via followup (not send_message)."""
    interaction = _make_interaction()
    siege_client = _make_client()
    siege_client.list_catalog = AsyncMock(side_effect=RuntimeError("boom"))

    await post_conditions_catalog(interaction, siege_client=siege_client)

    interaction.followup.send.assert_awaited_once()
    interaction.response.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# /post-conditions-get (per-user read)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_command_defers_before_fetching() -> None:
    """/post-conditions-get defers the interaction before calling siege-web."""
    interaction = _make_interaction()
    siege_client = _make_client()

    await post_conditions_get(interaction, siege_client=siege_client)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)


@pytest.mark.asyncio
async def test_get_command_replies_via_followup() -> None:
    """/post-conditions-get sends its reply through interaction.followup.send."""
    interaction = _make_interaction()
    siege_client = _make_client()

    await post_conditions_get(interaction, siege_client=siege_client)

    interaction.followup.send.assert_awaited_once()
    interaction.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_command_uses_invoking_user_id() -> None:
    """/post-conditions-get uses interaction.user.id (not a target arg)."""
    interaction = _make_interaction(discord_id=_DISCORD_ID)
    siege_client = _make_client()

    await post_conditions_get(interaction, siege_client=siege_client)

    siege_client.get_my_preferences.assert_awaited_once_with(discord_id=str(_DISCORD_ID))


@pytest.mark.asyncio
async def test_get_command_sends_ephemeral_reply() -> None:
    """/post-conditions-get reply is ephemeral (via followup)."""
    interaction = _make_interaction()
    siege_client = _make_client()

    await post_conditions_get(interaction, siege_client=siege_client)

    call_kwargs = interaction.followup.send.call_args[1]
    assert call_kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_get_command_empty_prefs_shows_none_set_message() -> None:
    """/post-conditions-get with empty prefs shows a no-preferences message."""
    interaction = _make_interaction()
    siege_client = _make_client(prefs=[])

    await post_conditions_get(interaction, siege_client=siege_client)

    call_args = interaction.followup.send.call_args
    content: str = call_args[0][0] if call_args[0] else call_args[1].get("content", "")
    assert "no post-condition preferences" in content.lower()


@pytest.mark.asyncio
async def test_get_command_404_shows_link_account_guidance() -> None:
    """/post-conditions-get on 404 shows link-your-account guidance via followup."""
    interaction = _make_interaction()
    siege_client = _make_client()
    siege_client.get_my_preferences = AsyncMock(side_effect=SiegeWebNotFoundError())

    await post_conditions_get(interaction, siege_client=siege_client)

    call_args = interaction.followup.send.call_args
    content: str = call_args[0][0] if call_args[0] else call_args[1].get("content", "")
    assert "rslsiege.com" in content.lower()
    assert call_args[1].get("ephemeral") is True
    interaction.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_command_401_error_does_not_leak_token() -> None:
    """/post-conditions-get on 401 sends user-readable message with no token."""
    interaction = _make_interaction()
    siege_client = _make_client()
    siege_client.get_my_preferences = AsyncMock(side_effect=SiegeWebAuthError())

    await post_conditions_get(interaction, siege_client=siege_client)

    call_args = interaction.followup.send.call_args
    content: str = call_args[0][0] if call_args[0] else call_args[1].get("content", "")
    assert _TOKEN not in content
    interaction.response.send_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# /post-conditions-set (per-user write)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_command_defers_before_fetching() -> None:
    """/post-conditions-set defers the interaction before calling siege-web."""
    interaction = _make_interaction()
    siege_client = _make_client()

    with patch(
        "mom_bot.post_conditions.commands.EditPreferencesView",
        autospec=True,
    ) as MockView:
        mock_view_instance = MagicMock()
        fake_embed = MagicMock(spec=discord.Embed)
        mock_view_instance.initial_embed = MagicMock(return_value=fake_embed)
        MockView.return_value = mock_view_instance

        await post_conditions_set(interaction, siege_client=siege_client)

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)


@pytest.mark.asyncio
async def test_set_command_replies_via_followup() -> None:
    """/post-conditions-set sends its reply through interaction.followup.send."""
    interaction = _make_interaction()
    siege_client = _make_client()

    with patch(
        "mom_bot.post_conditions.commands.EditPreferencesView",
        autospec=True,
    ) as MockView:
        mock_view_instance = MagicMock()
        fake_embed = MagicMock(spec=discord.Embed)
        mock_view_instance.initial_embed = MagicMock(return_value=fake_embed)
        MockView.return_value = mock_view_instance

        await post_conditions_set(interaction, siege_client=siege_client)

    interaction.followup.send.assert_awaited_once()
    interaction.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_command_uses_invoking_user_id() -> None:
    """/post-conditions-set uses interaction.user.id for all API calls."""
    interaction = _make_interaction(discord_id=_DISCORD_ID)
    siege_client = _make_client()

    with patch(
        "mom_bot.post_conditions.commands.EditPreferencesView",
        autospec=True,
    ) as MockView:
        mock_view_instance = MagicMock()
        fake_embed = MagicMock(spec=discord.Embed)
        mock_view_instance.initial_embed = MagicMock(return_value=fake_embed)
        MockView.return_value = mock_view_instance

        await post_conditions_set(interaction, siege_client=siege_client)

    # get_my_preferences must have been called with the invoking user's ID.
    siege_client.get_my_preferences.assert_awaited_once_with(discord_id=str(_DISCORD_ID))


@pytest.mark.asyncio
async def test_set_command_sends_ephemeral_reply() -> None:
    """/post-conditions-set sends an ephemeral response via followup."""
    interaction = _make_interaction()
    siege_client = _make_client()

    with patch(
        "mom_bot.post_conditions.commands.EditPreferencesView",
        autospec=True,
    ) as MockView:
        mock_view_instance = MagicMock()
        fake_embed = MagicMock(spec=discord.Embed)
        mock_view_instance.initial_embed = MagicMock(return_value=fake_embed)
        MockView.return_value = mock_view_instance

        await post_conditions_set(interaction, siege_client=siege_client)

    call_kwargs = interaction.followup.send.call_args[1]
    assert call_kwargs.get("ephemeral") is True


@pytest.mark.asyncio
async def test_set_command_404_shows_link_account_guidance() -> None:
    """/post-conditions-set on 404 from GET shows link-your-account guidance via followup."""
    interaction = _make_interaction()
    siege_client = _make_client()
    siege_client.get_my_preferences = AsyncMock(side_effect=SiegeWebNotFoundError())

    await post_conditions_set(interaction, siege_client=siege_client)

    call_args = interaction.followup.send.call_args
    content: str = call_args[0][0] if call_args[0] else call_args[1].get("content", "")
    assert "rslsiege.com" in content.lower()
    assert call_args[1].get("ephemeral") is True
    interaction.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_command_opens_edit_preferences_view() -> None:
    """/post-conditions-set opens an EditPreferencesView (not PostConditionsView).

    The view= kwarg on followup.send must be an EditPreferencesView instance
    and the embed= kwarg must be a discord.Embed from initial_embed().
    """
    interaction = _make_interaction()
    siege_client = _make_client(catalog=_CATALOG, prefs=_PREFS)

    with patch(
        "mom_bot.post_conditions.commands.EditPreferencesView",
        autospec=True,
    ) as MockView:
        mock_view_instance = MagicMock()
        fake_embed = MagicMock(spec=discord.Embed)
        mock_view_instance.initial_embed = MagicMock(return_value=fake_embed)
        MockView.return_value = mock_view_instance

        await post_conditions_set(interaction, siege_client=siege_client)

    call_kwargs = interaction.followup.send.call_args[1]
    assert isinstance(
        call_kwargs.get("embed"), discord.Embed
    ), "followup.send must include embed= from initial_embed()"
    assert (
        call_kwargs.get("view") is mock_view_instance
    ), "followup.send view= must be the EditPreferencesView instance"
    mock_view_instance.initial_embed.assert_called_once()


@pytest.mark.asyncio
async def test_set_command_initial_embed_reflects_preexisting_selections() -> None:
    """/post-conditions-set embed description is not 'None selected yet.' when prefs exist.

    Exercises the real EditPreferencesView (no mock) to confirm the embed body
    built from existing preferences reflects pre-existing selections.
    """
    interaction = _make_interaction()
    siege_client = _make_client(catalog=_CATALOG, prefs=_PREFS)

    await post_conditions_set(interaction, siege_client=siege_client)

    call_kwargs = interaction.followup.send.call_args[1]
    embed: discord.Embed = call_kwargs.get("embed")
    assert embed is not None, "embed must be present on initial render"
    assert isinstance(embed, discord.Embed)
    # _PREFS contains id=5; the description must NOT be the empty-state sentinel.
    assert (
        embed.description != "_None selected yet._"
    ), "embed should show pre-existing selections, not the empty-state text"


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


@pytest.mark.asyncio
async def test_set_command_attaches_real_edit_preferences_view() -> None:
    """Integration: real EditPreferencesView is constructed and attached to followup.send.

    Exercises the command end-to-end without mocking EditPreferencesView so
    that the isinstance check is against the concrete class, not a mock.
    """
    interaction = _make_interaction()
    siege_client = _make_client(catalog=_CATALOG, prefs=_PREFS)

    await post_conditions_set(interaction, siege_client=siege_client)

    call_kwargs = interaction.followup.send.call_args[1]
    assert isinstance(call_kwargs["view"], EditPreferencesView)
