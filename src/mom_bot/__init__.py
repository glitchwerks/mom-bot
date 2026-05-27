"""mom-bot — RAID guild Discord bot.

This package provides the Discord bot for the RAID guild. It is built on
discord.py and uses SQLAlchemy + Alembic for persistence.

The package version is read from the installed distribution metadata so
that ``pyproject.toml`` is the single source of truth.  In a source
checkout that has not been installed (e.g. some ad-hoc import scenarios),
``__version__`` falls back to ``"0.0.0+unknown"`` to signal the situation
without breaking the import.
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__: str = _pkg_version("mom-bot")
except PackageNotFoundError:
    # Source checkout without an editable install — fall back so that bare
    # imports still work.  The ``+unknown`` local-version marker makes it
    # easy to distinguish from a real release.
    __version__ = "0.0.0+unknown"
