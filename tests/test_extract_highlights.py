"""Tests for scripts/extract-highlights.sh — the Highlights-section extractor.

Each test pipes a fixture release body through the script via subprocess and
asserts on stdout.  The script is the unit under test; these tests act as the
harness described in the task spec (§ Optional but recommended).

Fixture scenarios:
  1. Highlights section present — happy path
  2. No Highlights section — fallback description returned
  3. Multi-paragraph Highlights block
  4. Oversized Highlights block (>1500 chars) — truncated with ellipsis + link
  5. Highlights block with emoji prefix (### 📣 Highlights)
  6. Highlights section followed immediately by another ## heading
  7. Highlights section at end of body (no following ## heading)
"""

import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "extract-highlights.sh"
FALLBACK_TAG = "v1.2.3"
FALLBACK_URL = "https://github.com/glitchwerks/mom-bot/releases/tag/v1.2.3"

# Resolve bash — prefer Git for Windows bash over WSL shim.
# On CI (ubuntu-latest) /usr/bin/bash is the standard location.
# On Windows dev machines (where WSL bash.exe is a shim), we prefer
# the Git-bundled bash at "C:/Program Files/Git/usr/bin/bash.exe".
_GIT_BASH = Path("C:/Program Files/Git/usr/bin/bash.exe")
BASH = str(_GIT_BASH) if _GIT_BASH.exists() else (shutil.which("bash") or "bash")


def run_script(body: str, tag: str = FALLBACK_TAG, url: str = FALLBACK_URL) -> str:
    """Pipe *body* through extract-highlights.sh and return stdout (stripped).

    Uses UTF-8 encoding explicitly so that emoji characters (e.g. 📣 U+1F4E3)
    are transmitted correctly even on Windows where the default console encoding
    is cp1252.  Also normalises CRLF -> LF in stdout so assertions don't need
    to care about platform line endings.
    """
    result = subprocess.run(
        [BASH, str(SCRIPT), tag, url],
        input=body,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"Script exited {result.returncode}; stderr: {result.stderr!r}"
    return result.stdout.replace("\r\n", "\n").strip()


# ---------------------------------------------------------------------------
# 1. Highlights present — happy path
# ---------------------------------------------------------------------------
BODY_WITH_HIGHLIGHTS = """\
## What's Changed

Some changelog detail.

### Highlights

This release ships the Discord notification workflow.
Members will now see an announcement when a new version is published.

### Fixed

- Bug #123 squashed.
"""


def test_highlights_present_returns_content():
    out = run_script(BODY_WITH_HIGHLIGHTS)
    assert "Discord notification workflow" in out
    assert "Members will now see" in out


def test_highlights_does_not_include_next_section():
    out = run_script(BODY_WITH_HIGHLIGHTS)
    assert "Bug #123 squashed" not in out


# ---------------------------------------------------------------------------
# 2. No Highlights section — fallback
# ---------------------------------------------------------------------------
BODY_WITHOUT_HIGHLIGHTS = """\
## What's Changed

- Refactored internals.
- Fixed a crash.

## Fixed

- Bug #456.
"""


def test_no_highlights_returns_fallback():
    out = run_script(BODY_WITHOUT_HIGHLIGHTS, tag=FALLBACK_TAG, url=FALLBACK_URL)
    assert FALLBACK_TAG in out
    assert FALLBACK_URL in out


def test_no_highlights_fallback_mentions_published():
    out = run_script(BODY_WITHOUT_HIGHLIGHTS, tag=FALLBACK_TAG, url=FALLBACK_URL)
    assert "published" in out.lower()


# ---------------------------------------------------------------------------
# 3. Multi-paragraph Highlights block
# ---------------------------------------------------------------------------
BODY_MULTI_PARA = """\
### 📣 Highlights

First paragraph with some context about this release.

Second paragraph with more detail.

Third paragraph wrapping it up.

### Changed

- Something changed.
"""


def test_multi_paragraph_highlights_all_captured():
    out = run_script(BODY_MULTI_PARA)
    assert "First paragraph" in out
    assert "Second paragraph" in out
    assert "Third paragraph" in out


# ---------------------------------------------------------------------------
# 4. Oversized Highlights block (>1500 chars) — must truncate
# ---------------------------------------------------------------------------
LONG_LINE = "A" * 200 + "\n"
BODY_OVERSIZED = "### Highlights\n\n" + LONG_LINE * 10 + "\n### Changed\n\n- x\n"
# 10 * 201 chars = 2010 chars of content — well above the 1500 cap


def test_oversized_highlights_truncated_to_1500():
    out = run_script(BODY_OVERSIZED, url=FALLBACK_URL)
    # The description portion (before "View full release notes") must be <=1500 chars
    # but after splitting the appended link line off we just check total length is reasonable
    assert len(out) <= 1600  # 1497 + "…" + newline + link line


def test_oversized_highlights_ends_with_ellipsis_and_link():
    out = run_script(BODY_OVERSIZED, url=FALLBACK_URL)
    # U+2026 HORIZONTAL ELLIPSIS — explicit unicode escape avoids file-encoding ambiguity
    assert "…" in out
    assert FALLBACK_URL in out


def test_oversized_highlights_link_on_own_line():
    out = run_script(BODY_OVERSIZED, url=FALLBACK_URL)
    lines = out.splitlines()
    # Last line should contain the full release notes URL
    assert FALLBACK_URL in lines[-1]


# ---------------------------------------------------------------------------
# 5. Highlights with emoji prefix
# ---------------------------------------------------------------------------
BODY_EMOJI_HIGHLIGHTS = """\
### 📣 Highlights

- Feature A is now live.
- Feature B shipped.

### Details

Internal changes.
"""


def test_emoji_prefixed_highlights_extracted():
    out = run_script(BODY_EMOJI_HIGHLIGHTS)
    assert "Feature A" in out
    assert "Feature B" in out


def test_emoji_highlights_does_not_include_details_section():
    out = run_script(BODY_EMOJI_HIGHLIGHTS)
    assert "Internal changes" not in out


# ---------------------------------------------------------------------------
# 6. Highlights section followed immediately by another ## heading (no blank line)
# ---------------------------------------------------------------------------
BODY_TIGHT = """\
### Highlights
Quick summary line.
### Fixed
- fix 1
"""


def test_tight_highlights_extracted():
    out = run_script(BODY_TIGHT)
    assert "Quick summary line" in out


def test_tight_highlights_does_not_bleed_into_fixed():
    out = run_script(BODY_TIGHT)
    assert "fix 1" not in out


# ---------------------------------------------------------------------------
# 7. Highlights at end of body (no following ## heading)
# ---------------------------------------------------------------------------
BODY_HIGHLIGHTS_LAST = """\
### Changed

- Something.

### Highlights

End-of-body summary with no following section.
"""


def test_highlights_at_end_of_body_extracted():
    out = run_script(BODY_HIGHLIGHTS_LAST)
    assert "End-of-body summary" in out
