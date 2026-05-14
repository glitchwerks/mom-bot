"""roles sub-package — Discord day-to-role mapping for mom-bot Epic 2.6.

Exports the ``DayRoleMap`` ORM model, the ``seed_day_role_map`` async
function used to populate and refresh the mapping table on bot startup, and
the ``apply_day_role`` service function + ``RoleSyncResult`` type used by
the sidecar endpoint (#65) and future slash commands.
"""

from mom_bot.roles.models import DayRoleMap
from mom_bot.roles.service import (
    REASON_ALREADY_HAS_ROLE,
    REASON_ALREADY_LACKS_ROLE,
    REASON_MEMBER_NOT_IN_GUILD,
    REASON_REMOVE_OTHER_DAY_FAILED_403,
    REASON_ROLE_NOT_SEEDED,
    RoleSyncResult,
    apply_day_role,
    run_preflight,
)

__all__ = [
    "DayRoleMap",
    "RoleSyncResult",
    "apply_day_role",
    "run_preflight",
    "REASON_MEMBER_NOT_IN_GUILD",
    "REASON_ROLE_NOT_SEEDED",
    "REASON_ALREADY_HAS_ROLE",
    "REASON_ALREADY_LACKS_ROLE",
    "REASON_REMOVE_OTHER_DAY_FAILED_403",
]
