"""roles sub-package — Discord day-to-role mapping for mom-bot Epic 2.6.

Exports the ``DayRoleMap`` ORM model and the ``seed_day_role_map`` async
function used to populate and refresh the mapping table on bot startup.
"""

from mom_bot.roles.models import DayRoleMap

__all__ = ["DayRoleMap"]
