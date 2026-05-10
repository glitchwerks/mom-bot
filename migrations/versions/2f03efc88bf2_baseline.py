"""baseline

Sequence: 0001 (baseline) — empty migration anchoring the alembic history.
Subsequent migrations use ``down_revision`` chains, not numeric prefixes.

Revision ID: 2f03efc88bf2
Revises:
Create Date: 2026-05-09 09:32:03.340170

"""

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401
from alembic import op  # noqa: F401

# revision identifiers, used by Alembic.
revision: str = "2f03efc88bf2"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply baseline migration (no-op — schema starts empty)."""
    pass


def downgrade() -> None:
    """Revert baseline migration (no-op — nothing to undo)."""
    pass
