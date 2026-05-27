"""Tests that mom_bot.__version__ matches the pyproject.toml version.

This test guards against the two-source-of-truth problem where
``pyproject.toml`` and ``__init__.py`` diverge silently on version bumps.
After the fix, ``__init__.py`` derives its value via
``importlib.metadata.version("mom-bot")`` so ``pyproject.toml`` is the
single source of truth.
"""

import tomllib
from pathlib import Path

import mom_bot


def _pyproject_version() -> str:
    """Read the raw version string from pyproject.toml.

    Returns:
        The ``[project] version`` string exactly as written in the file.
    """
    pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
    with pyproject_path.open("rb") as fh:
        data = tomllib.load(fh)
    return data["project"]["version"]  # type: ignore[no-any-return]


def _normalize_version(raw: str) -> str:
    """Normalise a PEP 440 version string to its canonical form.

    ``importlib.metadata`` returns the normalised form (e.g. ``1.0.0rc0``),
    while ``pyproject.toml`` may contain the non-normalised form
    (e.g. ``1.0.0-rc.0``).  Both represent the same version; this helper
    strips separators so both sides of the assertion use the same string.

    Args:
        raw: A version string, possibly containing ``-`` and ``.``
            separators (e.g. ``1.0.0-rc.0``).

    Returns:
        The version string with ``-`` and ``.`` separators removed from
        pre-release / post-release / dev segments (e.g. ``1.0.0rc0``).
    """
    # Replace hyphen-separated pre-release separator (1.0.0-rc.0 → 1.0.0rc0)
    # and dot-separated pre-release numeric (1.0.0rc.0 → 1.0.0rc0).
    import re

    # Normalise: remove hyphens before common pre-release labels, then
    # collapse any remaining dot before the trailing digit in pre-release.
    normalised = re.sub(
        r"[.\-](alpha|beta|rc|a|b)([.\-]?)(\d+)",
        lambda m: m.group(1) + m.group(3),
        raw,
        flags=re.IGNORECASE,
    )
    return normalised


class TestVersion:
    """Package version consistency tests."""

    def test_package_version_matches_pyproject(self) -> None:
        """mom_bot.__version__ must equal the version in pyproject.toml.

        After the single-source-of-truth refactor, ``__init__.py``
        derives its value from ``importlib.metadata.version("mom-bot")``,
        which reads the installed package metadata built from
        ``pyproject.toml``.  This test asserts they agree so that a
        version bump in ``pyproject.toml`` automatically propagates.
        """
        pyproject_raw = _pyproject_version()
        expected = _normalize_version(pyproject_raw)
        assert mom_bot.__version__ == expected, (
            f"mom_bot.__version__ ({mom_bot.__version__!r}) does not match "
            f"pyproject.toml version ({pyproject_raw!r}, normalised to "
            f"{expected!r}).  Update __init__.py or pyproject.toml so they "
            f"agree."
        )
