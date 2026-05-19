"""Discord slash command handlers for post-condition preferences.

Provides three ``app_commands`` handlers that proxy to siege-web's
per-member preferences API:

- :func:`post_conditions_catalog` (``/post-conditions``) — ephemeral catalog view.
- :func:`post_conditions_get` (``/post-conditions-get``) — per-user preference read.
- :func:`post_conditions_set` (``/post-conditions-set``) — per-user paginated set UI.

All three commands enforce **per-user scope** — they operate on the invoking
user's Discord ID (``interaction.user.id``) only.  There is no target-user
parameter and no admin override.

Usage
-----
Call :func:`register` once at bot startup to attach all three commands to
the command tree::

    from mom_bot.post_conditions.commands import register
    from mom_bot.post_conditions.client import SiegeWebClient

    siege_client = SiegeWebClient(
        base_url=load_secret("siege-web-url"),
        token=load_secret("siege-web-bot-token"),
    )
    register(tree=client.tree, siege_client=siege_client)
"""

from __future__ import annotations

import logging
from typing import Any

import discord
import discord.app_commands

from mom_bot.post_conditions.client import (
    SiegeWebAuthError,
    SiegeWebClient,
    SiegeWebNotFoundError,
)
from mom_bot.post_conditions.grouping import group_by_meta
from mom_bot.post_conditions.views import PostConditionsView

__all__ = [
    "post_conditions_catalog",
    "post_conditions_get",
    "post_conditions_set",
    "register",
]

_logger = logging.getLogger(__name__)

_LINK_YOUR_ACCOUNT_MSG = (
    "Your Discord account isn't registered in siege-web. "
    "Sign in once at https://rslsiege.com to link your account."
)

_OPS_ERROR_MSG = "An internal error occurred while contacting siege-web. " "Please try again later."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_catalog(
    conditions: list[dict[str, Any]],
) -> str:
    """Format a list of conditions as a grouped, human-readable string.

    Groups conditions by meta-category in :data:`META_GROUPS` order and
    renders each group as a Markdown section with a bullet list.

    Args:
        conditions: PostConditionResponse dicts to format.

    Returns:
        A Markdown-formatted string suitable for an ephemeral Discord message.
    """
    groups = group_by_meta(conditions)
    if not groups:
        return "No post-conditions found."

    lines: list[str] = []
    for meta_label, conds in groups:
        lines.append(f"**{meta_label}**")
        for cond in conds:
            lines.append(f"- {cond['description']}")
        lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Command handler functions
# ---------------------------------------------------------------------------


async def post_conditions_catalog(
    interaction: discord.Interaction,
    *,
    siege_client: SiegeWebClient,
) -> None:
    """Handle ``/post-conditions`` — show the full post-condition catalog.

    Calls the open catalog endpoint (no auth), groups by meta-category, and
    sends an ephemeral reply.

    Args:
        interaction: The Discord slash-command interaction.
        siege_client: The siege-web HTTP client instance.
    """
    try:
        catalog = await siege_client.list_catalog()
    except Exception:
        _logger.exception("Failed to fetch post-condition catalog from siege-web.")
        await interaction.response.send_message(_OPS_ERROR_MSG, ephemeral=True)
        return

    content = _format_catalog(catalog)
    await interaction.response.send_message(content, ephemeral=True)


async def post_conditions_get(
    interaction: discord.Interaction,
    *,
    siege_client: SiegeWebClient,
) -> None:
    """Handle ``/post-conditions-get`` — show the invoking user's preferences.

    Reads the invoking user's Discord ID from ``interaction.user.id`` and
    fetches their current preferences.  Surfaces a link-your-account message
    on 404 and a generic ops-error message on 401.  No target-user parameter.

    Args:
        interaction: The Discord slash-command interaction.
        siege_client: The siege-web HTTP client instance.
    """
    discord_id = str(interaction.user.id)

    try:
        prefs = await siege_client.get_my_preferences(discord_id=discord_id)
    except SiegeWebNotFoundError:
        await interaction.response.send_message(_LINK_YOUR_ACCOUNT_MSG, ephemeral=True)
        return
    except SiegeWebAuthError:
        _logger.error(
            "Auth error fetching preferences for discord_id=%s",
            discord_id,
        )
        await interaction.response.send_message(_OPS_ERROR_MSG, ephemeral=True)
        return
    except Exception:
        _logger.exception(
            "Unexpected error fetching preferences for discord_id=%s",
            discord_id,
        )
        await interaction.response.send_message(_OPS_ERROR_MSG, ephemeral=True)
        return

    if not prefs:
        await interaction.response.send_message(
            "You have no post-condition preferences set.",
            ephemeral=True,
        )
        return

    content = _format_catalog(prefs)
    await interaction.response.send_message(content, ephemeral=True)


async def post_conditions_set(
    interaction: discord.Interaction,
    *,
    siege_client: SiegeWebClient,
) -> None:
    """Handle ``/post-conditions-set`` — open the paginated preference editor.

    Fetches both the full catalog and the user's current preferences, then
    opens a :class:`~mom_bot.post_conditions.views.PostConditionsView`
    pre-populated with the user's existing selections.  404 on the initial
    GET surfaces a link-your-account message without opening the view.

    Args:
        interaction: The Discord slash-command interaction.
        siege_client: The siege-web HTTP client instance.
    """
    discord_id = str(interaction.user.id)

    try:
        catalog, prefs = (
            await siege_client.list_catalog(),
            await siege_client.get_my_preferences(discord_id=discord_id),
        )
    except SiegeWebNotFoundError:
        await interaction.response.send_message(_LINK_YOUR_ACCOUNT_MSG, ephemeral=True)
        return
    except SiegeWebAuthError:
        _logger.error(
            "Auth error opening set-preferences view for discord_id=%s",
            discord_id,
        )
        await interaction.response.send_message(_OPS_ERROR_MSG, ephemeral=True)
        return
    except Exception:
        _logger.exception(
            "Unexpected error opening set-preferences view for discord_id=%s",
            discord_id,
        )
        await interaction.response.send_message(_OPS_ERROR_MSG, ephemeral=True)
        return

    view = PostConditionsView(
        catalog=catalog,
        initial_prefs=prefs,
        discord_id=discord_id,
        siege_client=siege_client,
    )
    header = view.build_header()
    await interaction.response.send_message(content=header, view=view, ephemeral=True)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(
    tree: discord.app_commands.CommandTree,
    siege_client: SiegeWebClient,
) -> None:
    """Attach all three post-condition commands to the given command tree.

    Commands registered:

    - ``/post-conditions``      — catalog view (open, ephemeral).
    - ``/post-conditions-get``  — per-user preference read (ephemeral).
    - ``/post-conditions-set``  — per-user paginated preference editor (ephemeral).

    Args:
        tree: The discord.py :class:`~discord.app_commands.CommandTree` to
            register commands onto.
        siege_client: A fully-configured :class:`SiegeWebClient` instance.
            The same instance is captured by all three command closures.
    """

    @tree.command(
        name="post-conditions",
        description="View all available post-condition categories (catalog).",
    )
    async def _catalog(interaction: discord.Interaction) -> None:
        """Show the full post-condition catalog, grouped by category."""
        await post_conditions_catalog(interaction, siege_client=siege_client)

    @tree.command(
        name="post-conditions-get",
        description="View your current post-condition preferences.",
    )
    async def _get(interaction: discord.Interaction) -> None:
        """Show your post-condition preferences, grouped by category."""
        await post_conditions_get(interaction, siege_client=siege_client)

    @tree.command(
        name="post-conditions-set",
        description="Set your post-condition preferences (paginated selector).",
    )
    async def _set(interaction: discord.Interaction) -> None:
        """Open the paginated post-condition preference editor."""
        await post_conditions_set(interaction, siege_client=siege_client)
