"""Seed-on-boot logic for the reminder scheduler (plan § 4).

Provides :func:`_maybe_seed_reminders`, which inserts the two default
reminders (Hydra and Chimera) into the ``reminders`` table the first time
the bot boots with an empty table.

Design constraints (locked, plan § 3 row 13):

- **Idempotent**: only runs when ``SELECT count(*) FROM reminders == 0``.
  Subsequent boots are a no-op.
- **Env-aware**: secret names are prefixed by ``MOM_BOT_ENV`` (``dev-``
  or ``prod-``) automatically by :func:`~mom_bot.config.load_secret`.
- **KV failure exits cleanly**: if any :func:`~mom_bot.config.load_secret`
  call raises, the function logs CRITICAL and re-raises so the process
  terminates.  Container Apps will restart the pod, which retries KV on
  the next boot.  The bot must never start ticking with an empty table.

Guild resolution (#49):

- The KV secret ``guild-id`` stores the Discord guild (server) snowflake
  for this environment (e.g. ``"1234567890"``).
- The bot account may be a member of multiple guilds simultaneously (e.g.
  the same bot identity is invited to both the dev and prod servers).
  ``bot.guilds[0]`` is non-deterministic in that case — the per-env
  ``guild-id`` KV secret is the authoritative selector.
- Resolved via ``bot.get_guild(int(load_secret("guild-id")))``.  If the
  result is ``None``, the bot is not a member of the configured guild and
  the process must exit cleanly rather than inserting a bad row.

Channel resolution (#47):

- The KV secret ``reminder-channel-name`` stores the channel name as a
  plain string (e.g. ``"reminders"``).
- At first boot, the function resolves that name to a snowflake via
  ``discord.utils.get(guild.text_channels, name=channel_name)`` where
  ``guild`` is the result of the guild-resolution step above.
- The resolved snowflake is stored in the ``channel_id`` DB column.
- The resolution happens once — channel renames after the first successful
  seed require a manual SQL UPDATE:
  ``UPDATE reminders SET channel_id = <new-snowflake> WHERE name = '<X>'``

Message templates are verbatim copies from ``clan_reminders.py:L128-L132``
(Hydra) and ``L141-L145`` (Chimera) per the plan's pre-work checklist.

.. note::

    ``role_mention_id`` is intentionally ``None`` for both seeded rows
    (#45). Reminders post to a dedicated channel and do not ping any role —
    the channel itself is the audience signal.  If a future need arises to
    re-add a mention for a specific reminder, use
    ``UPDATE reminders SET role_mention_id = <snowflake> WHERE name = '<X>'``
    without touching this function.
"""

from __future__ import annotations

import datetime
import logging

import discord
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mom_bot.config import ConfigError, load_secret
from mom_bot.reminders.models import Reminder

__all__ = ["_maybe_seed_reminders"]

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Verbatim message templates (clan_reminders.py:L128-L132 and L141-L145)
# ---------------------------------------------------------------------------

HYDRA_TEMPLATE: str = (
    ":dragon_face: **Hydra Reminder!** :dragon_face:\n"
    "There are less than 24 hours left to do your Hydra keys!\n"
    "Don't forget to hit the boss and help the clan!"
)

CHIMERA_TEMPLATE: str = (
    ":japanese_ogre: **Chimera Reminder!** :japanese_ogre:\n"
    "There are less than 24 hours left to do your Chimera attempts!\n"
    "Make sure to participate and help the clan!"
)


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------


def _maybe_seed_reminders(
    session: Session,
    bot: discord.Client,
) -> None:
    """Insert Hydra + Chimera reminder rows if the table is empty.

    Reads two Key Vault secrets via :func:`~mom_bot.config.load_secret`
    (which applies the ``MOM_BOT_ENV`` prefix automatically):

    - ``guild-id`` — Discord guild (server) snowflake for this environment.
      The bot account may be invited to multiple guilds simultaneously (e.g.
      dev + prod); this secret selects the correct one deterministically
      (#49).  Resolved via ``bot.get_guild(int(guild_id))``.
    - ``reminder-channel-name`` — Discord channel name (string) used for
      both Hydra and Chimera reminders.  Resolved to a snowflake at first
      boot via ``discord.utils.get(guild.text_channels, name=channel_name)``
      within the guild identified by ``guild-id``.  Both reminders fire to
      the same channel per env.

    The resolved channel snowflake is written into the ``channel_id`` DB
    column.  If the channel is renamed after first seed, use
    ``UPDATE reminders SET channel_id = <new-snowflake> WHERE name = '<X>'``
    directly without re-seeding.

    Both Hydra and Chimera seed with ``role_mention_id=None`` — reminders
    post to the channel without pinging any role (#45).  If a future need
    arises to split channels, either add per-reminder secrets and update this
    function, or ``UPDATE reminders SET channel_id = ... WHERE name =
    'Chimera'`` directly without re-seeding.

    Schedule:

    - Hydra fires **Tuesday** (weekday=1) at **07:00 UTC**.
    - Chimera fires **Wednesday** (weekday=2) at **12:00 UTC**.

    Args:
        session: An open :class:`~sqlalchemy.orm.Session` with write access
            to the ``reminders`` table.
        bot: The connected :class:`discord.Client` instance.  Must have
            already received the READY event so that
            :meth:`~discord.Client.get_guild` can resolve the guild from the
            internal cache.  Used to resolve guild and channel name to a
            snowflake.

    Raises:
        ConfigError: If any KV secret is missing, if the bot is not a member
            of the configured guild, or if the named channel does not exist
            in that guild.
        Exception: Any other exception raised by
            :func:`~mom_bot.config.load_secret` is logged at CRITICAL and
            re-raised so the process exits.
    """
    count = session.scalar(select(func.count(Reminder.id)))
    if count != 0:
        _logger.debug(
            "_maybe_seed_reminders: table non-empty (%d rows); skipping",
            count,
        )
        return

    _logger.info("_maybe_seed_reminders: table empty; seeding Hydra + Chimera")

    try:
        guild_id = int(load_secret("guild-id"))
        channel_name = load_secret("reminder-channel-name")
    except Exception as exc:
        secret_name = getattr(exc, "secret_name", "unknown")
        _logger.critical(
            "_maybe_seed_reminders: failed to load KV secret %r — "
            "bot cannot seed reminders; exiting. Error: %s",
            secret_name,
            exc,
        )
        raise

    # Selects the guild whose ID matches the env's ``<env>-guild-id`` KV
    # secret.  The bot account may be in multiple guilds simultaneously
    # (e.g. dev + prod); the per-env config picks the right one
    # deterministically (#49).  ``bot.get_guild()`` returning None covers
    # both "bot has zero guilds" and "bot is in some guilds but not the
    # configured one" — one error path is cleaner than two.
    guild = bot.get_guild(guild_id)
    if guild is None:
        _logger.critical(
            "_maybe_seed_reminders: bot is not a member of guild id=%d "
            "(KV `<env>-guild-id`). Either invite the bot to that guild, "
            "or fix the KV secret value.",
            guild_id,
        )
        raise ConfigError(
            message=(f"Bot is not a member of guild id={guild_id} " "(from KV `<env>-guild-id`).")
        )

    channel = discord.utils.get(guild.text_channels, name=channel_name)
    if channel is None:
        _logger.critical(
            "_maybe_seed_reminders: channel %r not found in guild %r (id=%d)",
            channel_name,
            guild.name,
            guild.id,
        )
        raise ConfigError(
            message=f"Reminder channel '{channel_name}' not found in guild '{guild.name}'"
        )

    channel_id: int = channel.id

    session.add_all(
        [
            Reminder(
                name="Hydra",
                channel_id=channel_id,
                weekday=1,
                fire_time_utc=datetime.time(7, 0, 0),
                message_template=HYDRA_TEMPLATE,
                role_mention_id=None,
            ),
            Reminder(
                name="Chimera",
                channel_id=channel_id,
                weekday=2,
                fire_time_utc=datetime.time(12, 0, 0),
                message_template=CHIMERA_TEMPLATE,
                role_mention_id=None,
            ),
        ]
    )
    session.commit()
    _logger.info(
        "_maybe_seed_reminders: seeded Hydra and Chimera (channel=%r → id=%d, no role mention)",
        channel_name,
        channel_id,
    )
