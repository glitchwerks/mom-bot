"""Verify alembic env.py wiring imports the package's Base correctly."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

# Resolve alembic.ini relative to this test file so the path works on both
# Windows dev machines and Linux CI runners.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = str(_REPO_ROOT / "alembic.ini")


def test_alembic_can_load_config() -> None:
    """Alembic.ini parses, migrations dir resolves, and baseline revision exists.

    Proves that:

    - ``alembic.ini`` is present and parseable.
    - ``migrations/`` directory is found by the script directory.
    - Exactly one revision exists (the empty baseline).

    If ``Base`` import in ``env.py`` is broken, this test fails because
    the migration module cannot be imported during revision discovery.
    """
    cfg = Config(_ALEMBIC_INI)
    script = ScriptDirectory.from_config(cfg)
    revisions = list(script.walk_revisions())
    assert len(revisions) == 1, "Expected exactly the baseline revision"
