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

Message templates are verbatim copies from ``clan_reminders.py:L128-L132``
(Hydra) and ``L141-L145`` (Chimera) per the plan's pre-work checklist.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from mom_bot.config import load_secret
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


def _maybe_seed_reminders(session: Session) -> None:
    """Insert Hydra + Chimera reminder rows if the table is empty.

    Reads three Key Vault secrets via :func:`~mom_bot.config.load_secret`
    (which applies the ``MOM_BOT_ENV`` prefix automatically):

    - ``reminder-hydra-channel-id`` — Discord channel snowflake for Hydra.
    - ``reminder-chimera-channel-id`` — Discord channel snowflake for Chimera.
    - ``reminder-mention-role-id`` — Discord role snowflake to mention.

    Both Hydra and Chimera share the single mention-role today (matching the
    source bot's ``Member`` default at ``clan_reminders.py:L107``).

    Schedule:

    - Hydra fires **Tuesday** (weekday=1) at **07:00 UTC**.
    - Chimera fires **Wednesday** (weekday=2) at **12:00 UTC**.

    Args:
        session: An open :class:`~sqlalchemy.orm.Session` with write access
            to the ``reminders`` table.

    Raises:
        Exception: Any exception raised by :func:`~mom_bot.config.load_secret`
            is logged at CRITICAL and re-raised so the process exits.
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
        hydra_channel = int(load_secret("reminder-hydra-channel-id"))
        chimera_channel = int(load_secret("reminder-chimera-channel-id"))
        mention_role = int(load_secret("reminder-mention-role-id"))
    except Exception as exc:
        secret_name = getattr(exc, "secret_name", "reminder-*")
        _logger.critical(
            "_maybe_seed_reminders: failed to load KV secret %r — "
            "bot cannot seed reminders; exiting. Error: %s",
            secret_name,
            exc,
        )
        raise

    session.add_all(
        [
            Reminder(
                name="Hydra",
                channel_id=hydra_channel,
                weekday=1,
                fire_time_utc=datetime.time(7, 0, 0),
                message_template=HYDRA_TEMPLATE,
                role_mention_id=mention_role,
            ),
            Reminder(
                name="Chimera",
                channel_id=chimera_channel,
                weekday=2,
                fire_time_utc=datetime.time(12, 0, 0),
                message_template=CHIMERA_TEMPLATE,
                role_mention_id=mention_role,
            ),
        ]
    )
    session.commit()
    _logger.info(
        "_maybe_seed_reminders: seeded Hydra (channel=%d) " "and Chimera (channel=%d) with role=%d",
        hydra_channel,
        chimera_channel,
        mention_role,
    )
