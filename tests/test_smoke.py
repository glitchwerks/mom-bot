"""Smoke test — verifies the package is importable and version is correct."""

import mom_bot


def test_version() -> None:
    """Assert the package version is a non-empty string.

    The exact value is validated in ``tests/test_version.py`` against
    ``pyproject.toml``.  This smoke test only guards that the attribute
    exists and is non-empty so the import is known-good.
    """
    assert isinstance(mom_bot.__version__, str)
    assert mom_bot.__version__
