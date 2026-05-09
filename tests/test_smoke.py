"""Smoke test — verifies the package is importable and version is correct."""

import mom_bot


def test_version() -> None:
    """Assert the package version matches the declared version string.

    This test exists to confirm that the package is importable and that
    the version attribute is set to the expected baseline value. It also
    keeps the pytest job from being a no-op in CI until real tests land.
    """
    assert mom_bot.__version__ == "0.0.1"
