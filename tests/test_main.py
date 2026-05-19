"""Tests for mom_bot.main — Discord client construction and /ping command.

TDD: these tests were written before the implementation.  Each test covers one
discrete behaviour of ``main.py``; run them first to confirm they all fail
(ImportError), then implement the module to make them green.

Design decisions:
- No live Discord connection is attempted in any test.
- ``load_secret`` is patched out so no Key Vault round-trip occurs.
- ``discord.Interaction.response.send_message`` is mocked via AsyncMock
  because discord.py defines it as a coroutine.
- ``make_client`` now accepts an optional ``siege_client`` parameter.
  Tests pass a :class:`~unittest.mock.MagicMock` to avoid Key Vault calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

# ---------------------------------------------------------------------------
# Test 1 — build_intents returns exactly the locked intent set
# ---------------------------------------------------------------------------


def test_build_intents_locked_set() -> None:
    """build_intents() must enable exactly guilds, members, guild_scheduled_events.

    Verifies the intent bitfield matches the lock spec from Epic 0 session
    decisions. No extra intents (MESSAGE_CONTENT, GUILD_PRESENCES, etc.) should
    be set.
    """
    from mom_bot.main import build_intents

    intents = build_intents()

    # Build the expected flags independently for comparison.
    expected = discord.Intents.none()
    expected.guilds = True
    expected.members = True
    expected.guild_scheduled_events = True

    assert intents.value == expected.value, (
        f"Intent bitfield mismatch: got {intents.value!r}, " f"expected {expected.value!r}"
    )

    # Explicitly confirm the three required flags and two common extras are off.
    assert intents.guilds is True
    assert intents.members is True
    assert intents.guild_scheduled_events is True
    assert intents.message_content is False
    assert intents.presences is False


# ---------------------------------------------------------------------------
# Test 2 — make_client registers /ping in the command tree
# ---------------------------------------------------------------------------


def test_make_client_registers_ping_command() -> None:
    """make_client() must register a command named 'ping' in the tree.

    Instantiates the client without running or connecting, then inspects the
    app_commands.CommandTree to confirm /ping was registered.  A mock
    SiegeWebClient is passed to avoid Key Vault round-trips.
    """
    from mom_bot.main import make_client

    client = make_client(siege_client=MagicMock())
    command_names = [cmd.name for cmd in client.tree.get_commands()]

    assert "ping" in command_names, f"Expected 'ping' in command tree; found: {command_names!r}"


# ---------------------------------------------------------------------------
# Test 3 — /ping callback produces correctly formatted response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ping_response_format() -> None:
    """The /ping callback must reply with pong!, version=, and uptime= substrings.

    Invokes the registered callback directly using a mock Interaction so no
    Discord connection is required.  The response send_message coroutine is
    replaced with AsyncMock to capture the message text.
    """
    from mom_bot.main import make_client

    client = make_client(siege_client=MagicMock())

    # Locate the /ping command in the tree.
    ping_cmd = next(
        (cmd for cmd in client.tree.get_commands() if cmd.name == "ping"),
        None,
    )
    assert ping_cmd is not None, "ping command not found in tree"

    # Build a mock Interaction with an async send_message.
    mock_interaction = MagicMock(spec=discord.Interaction)
    mock_interaction.response = MagicMock()
    mock_interaction.response.send_message = AsyncMock()

    # Invoke the callback directly (bypasses gateway/connection entirely).
    await ping_cmd.callback(mock_interaction)

    # Assert send_message was called exactly once.
    mock_interaction.response.send_message.assert_called_once()

    # Extract the message text from the call args.
    call_args = mock_interaction.response.send_message.call_args
    message: str = call_args.args[0] if call_args.args else ""

    assert "pong!" in message, f"Expected 'pong!' in response; got: {message!r}"
    assert "version=" in message, f"Expected 'version=' in response; got: {message!r}"
    assert "uptime=" in message, f"Expected 'uptime=' in response; got: {message!r}"


# ---------------------------------------------------------------------------
# Test 4 — make_client registers post-condition commands
# ---------------------------------------------------------------------------


def test_make_client_registers_post_condition_commands() -> None:
    """make_client() must register the three post-condition slash commands.

    Verifies that post-conditions, post-conditions-get, and
    post-conditions-set are all present in the command tree after
    ``make_client`` returns.  A mock SiegeWebClient is passed to avoid
    Key Vault round-trips.
    """
    from mom_bot.main import make_client

    client = make_client(siege_client=MagicMock())
    command_names = {cmd.name for cmd in client.tree.get_commands()}

    assert (
        "post-conditions" in command_names
    ), f"Expected 'post-conditions' in command tree; found: {command_names!r}"
    assert (
        "post-conditions-get" in command_names
    ), f"Expected 'post-conditions-get' in command tree; found: {command_names!r}"
    assert (
        "post-conditions-set" in command_names
    ), f"Expected 'post-conditions-set' in command tree; found: {command_names!r}"


# ---------------------------------------------------------------------------
# Test 5 — make_client stores siege_client on the bot for shutdown
# ---------------------------------------------------------------------------


def test_make_client_stores_siege_client_on_bot() -> None:
    """make_client() must store the siege_client on the bot for shutdown.

    :meth:`MomBot.close` calls ``siege_client.close()`` on shutdown.
    This requires the client to be stored on the bot instance.
    """
    from mom_bot.main import make_client

    mock_siege = MagicMock()
    bot = make_client(siege_client=mock_siege)

    assert (
        bot._siege_client is mock_siege
    ), "Expected make_client to store siege_client on bot._siege_client"


# ---------------------------------------------------------------------------
# Test 6 — MomBot.close() calls siege_client.close()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mom_bot_close_calls_siege_client_close() -> None:
    """MomBot.close() must call siege_client.close() on shutdown.

    Verifies the shutdown lifecycle: :meth:`MomBot.close` must await
    ``_siege_client.close()`` so the aiohttp session is released.
    """
    from mom_bot.main import make_client

    mock_siege = MagicMock()
    mock_siege.close = AsyncMock()

    bot = make_client(siege_client=mock_siege)

    # Patch discord.Client.close so we don't need a live gateway.
    with patch("discord.Client.close", new_callable=AsyncMock):
        await bot.close()

    mock_siege.close.assert_called_once()
