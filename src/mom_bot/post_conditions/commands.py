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

import discord
import discord.app_commands

from mom_bot.post_conditions.client import (
    SiegeWebAuthError,
    SiegeWebClient,
    SiegeWebNotFoundError,
)
from mom_bot.post_conditions.grouping import group_by_meta
from mom_bot.post_conditions.views import PostConditionsGridView, build_grouped_embed

__all__ = [
    "post_conditions_catalog",
    "post_conditions_get",
    "post_conditions_set",
    "register",
]

_logger = logging.getLogger(__name__)

_LINK_YOUR_ACCOUNT_MSG = (
    "Your Discord account isn't registered with siege-web yet. "
    "Ask a clan admin to add you, then try this command again."
)

_OPS_ERROR_MSG = "An internal error occurred while contacting siege-web. " "Please try again later."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Command handler functions
# ---------------------------------------------------------------------------


async def post_conditions_catalog(
    interaction: discord.Interaction,
    *,
    siege_client: SiegeWebClient,
) -> None:
    """Handle ``/post-conditions`` — show the full post-condition catalog.

    Defers the interaction immediately to satisfy Discord's 3-second deadline,
    then calls the open catalog endpoint (no auth), groups by meta-category,
    and sends an ephemeral followup reply.

    Args:
        interaction: The Discord slash-command interaction.
        siege_client: The siege-web HTTP client instance.
    """
    await interaction.response.defer(ephemeral=True)

    try:
        catalog = await siege_client.list_catalog()
    except Exception:
        _logger.exception("Failed to fetch post-condition catalog from siege-web.")
        await interaction.followup.send(_OPS_ERROR_MSG, ephemeral=True)
        return

    if not catalog:
        await interaction.followup.send("No post-conditions found.", ephemeral=True)
        return

    pages = group_by_meta(catalog)
    selected_ids = {int(c["id"]) for c in catalog}
    embed = build_grouped_embed(
        title="Post-condition catalog",
        pages=pages,
        selected_ids=selected_ids,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


async def post_conditions_get(
    interaction: discord.Interaction,
    *,
    siege_client: SiegeWebClient,
) -> None:
    """Handle ``/post-conditions-get`` — show the invoking user's preferences.

    Defers the interaction immediately to satisfy Discord's 3-second deadline,
    then fetches the invoking user's current preferences.  Surfaces a
    link-your-account message on 404 and a generic ops-error message on 401.
    No target-user parameter.

    Args:
        interaction: The Discord slash-command interaction.
        siege_client: The siege-web HTTP client instance.
    """
    discord_id = str(interaction.user.id)
    discord_username = interaction.user.name

    await interaction.response.defer(ephemeral=True)

    try:
        prefs = await siege_client.get_my_preferences(
            discord_id=discord_id,
            discord_username=discord_username,
        )
    except SiegeWebNotFoundError:
        await interaction.followup.send(_LINK_YOUR_ACCOUNT_MSG, ephemeral=True)
        return
    except SiegeWebAuthError:
        _logger.error(
            "Auth error fetching preferences for discord_id=%s",
            discord_id,
        )
        await interaction.followup.send(_OPS_ERROR_MSG, ephemeral=True)
        return
    except Exception:
        _logger.exception(
            "Unexpected error fetching preferences for discord_id=%s",
            discord_id,
        )
        await interaction.followup.send(_OPS_ERROR_MSG, ephemeral=True)
        return

    if not prefs:
        await interaction.followup.send(
            "You have no post-condition preferences set.",
            ephemeral=True,
        )
        return

    pages = group_by_meta(prefs)
    selected_ids = {int(p["id"]) for p in prefs}
    embed = build_grouped_embed(
        title="Your post-condition preferences",
        pages=pages,
        selected_ids=selected_ids,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


async def post_conditions_set(
    interaction: discord.Interaction,
    *,
    siege_client: SiegeWebClient,
) -> None:
    """Handle ``/post-conditions-set`` — open the checkbox preference editor.

    Defers the interaction immediately to satisfy Discord's 3-second deadline.
    Fetches both the full catalog and the user's current preferences, then
    opens a :class:`~mom_bot.post_conditions.views.PostConditionsGridView`
    pre-populated with the user's existing selections.  404 on the initial
    GET surfaces a link-your-account message without opening the view.

    Note: SiegeWebNotFoundError from list_catalog would surface here as
    "account not registered" — misleading but unreachable in practice
    (catalog endpoint never 404s for valid base_url).

    Args:
        interaction: The Discord slash-command interaction.
        siege_client: The siege-web HTTP client instance.
    """
    discord_id = str(interaction.user.id)
    discord_username = interaction.user.name

    await interaction.response.defer(ephemeral=True)

    try:
        catalog, prefs = (
            await siege_client.list_catalog(),
            await siege_client.get_my_preferences(
                discord_id=discord_id,
                discord_username=discord_username,
            ),
        )
    except SiegeWebNotFoundError:
        await interaction.followup.send(_LINK_YOUR_ACCOUNT_MSG, ephemeral=True)
        return
    except SiegeWebAuthError:
        _logger.error(
            "Auth error opening set-preferences view for discord_id=%s",
            discord_id,
        )
        await interaction.followup.send(_OPS_ERROR_MSG, ephemeral=True)
        return
    except Exception:
        _logger.exception(
            "Unexpected error opening set-preferences view for discord_id=%s",
            discord_id,
        )
        await interaction.followup.send(_OPS_ERROR_MSG, ephemeral=True)
        return

    # PostConditionsGridView expects preference IDs (list[int]), not full dicts.
    pref_ids = [int(p["id"]) for p in prefs]
    view = PostConditionsGridView(
        catalog=catalog,
        preferences=pref_ids,
        discord_id=discord_id,
        discord_username=discord_username,
        siege_client=siege_client,
    )
    await interaction.followup.send(
        embed=view.initial_embed(),
        view=view,
        ephemeral=True,
    )


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
