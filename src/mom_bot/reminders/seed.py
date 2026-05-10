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

Channel resolution (#47):

- The KV secret ``reminder-channel-name`` stores the channel name as a
  plain string (e.g. ``"reminders"``).
- At first boot, the function resolves that name to a snowflake via
  ``discord.utils.get(bot.guilds[0].text_channels, name=channel_name)``.
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

    Reads one Key Vault secret via :func:`~mom_bot.config.load_secret`
    (which applies the ``MOM_BOT_ENV`` prefix automatically):

    - ``reminder-channel-name`` — Discord channel name (string) used for
      both Hydra and Chimera reminders.  Resolved to a snowflake at first
      boot via the connected discord.py client.  Both reminders fire to the
      same channel per env.

    Channel resolution uses ``discord.utils.get(bot.guilds[0].text_channels,
    name=channel_name)``.  The resolved snowflake is written into the
    ``channel_id`` DB column.  If the channel is renamed after first seed,
    use ``UPDATE reminders SET channel_id = <new-snowflake> WHERE name =
    '<X>'`` directly without re-seeding.

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
            already received the READY event so that ``bot.guilds`` is
            populated.  Used to resolve the channel name to a snowflake.

    Raises:
        ConfigError: If the KV secret is missing, if the bot has no guilds,
            or if the named channel does not exist in the guild.
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
        channel_name = load_secret("reminder-channel-name")
    except Exception as exc:
        secret_name = getattr(exc, "secret_name", "reminder-channel-name")
        _logger.critical(
            "_maybe_seed_reminders: failed to load KV secret %r — "
            "bot cannot seed reminders; exiting. Error: %s",
            secret_name,
            exc,
        )
        raise

    if not bot.guilds:
        _logger.critical(
            "_maybe_seed_reminders: bot has no guilds; gateway READY " "did not populate the cache"
        )
        _err: ConfigError = ConfigError.__new__(ConfigError)
        Exception.__init__(
            _err,
            "Bot has no guilds at seed time" " — gateway READY ordering bug?",
        )
        raise _err

    guild = bot.guilds[0]
    channel = discord.utils.get(guild.text_channels, name=channel_name)
    if channel is None:
        _logger.critical(
            "_maybe_seed_reminders: channel %r not found in guild %r " "(id=%d)",
            channel_name,
            guild.name,
            guild.id,
        )
        _err = ConfigError.__new__(ConfigError)
        Exception.__init__(
            _err,
            f"Reminder channel '{channel_name}' not found in guild" f" '{guild.name}'",
        )
        raise _err

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
        "_maybe_seed_reminders: seeded Hydra and Chimera" " (channel=%r → id=%d, no role mention)",
        channel_name,
        channel_id,
    )
