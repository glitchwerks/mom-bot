#!/usr/bin/env bash
# extract-highlights.sh — Extract the Highlights section from a GitHub Release body.
#
# Usage:
#   echo "$RELEASE_BODY" | bash scripts/extract-highlights.sh <tag_name> <html_url>
#
# Arguments:
#   $1  tag_name  — e.g. "v1.2.3"  (used in fallback description)
#   $2  html_url  — e.g. "https://github.com/glitchwerks/mom-bot/releases/tag/v1.2.3"
#                   (used in fallback and in the truncation link)
#
# Output (stdout):
#   If a "### [📣 ]Highlights" section is found in stdin:
#     - All text under that heading up to the next "## " heading or EOF.
#     - Trimmed of leading/trailing whitespace.
#     - Soft-capped at 1500 chars: if the extracted text exceeds 1497 chars,
#       it is truncated to 1497 chars + "…" and a new line appended:
#       "View full release notes: <html_url>"
#   If no Highlights section is found:
#     - "<tag_name> published. View notes: <html_url>"
#
# Exit code: always 0 (the workflow MUST still post on missing Highlights).
#
# Regex notes:
#   - Matches "### Highlights" or "### 📣 Highlights" (case-insensitive on the word).
#   - \b-equivalent: [[:space:]]|$ after the word prevents false matches on
#     headings like "### Highlights and Lowlights" (well, it does include those;
#     for this use-case that's acceptable — any heading starting with Highlights
#     is fair game for extraction).
#
# Truncation:
#   - Soft-cap is 1500 characters (readability limit; Discord hard limit 4096).
#   - Uses awk's length() which counts characters (multibyte-aware in gawk;
#     byte-count in mawk/nawk — on ubuntu-latest, /usr/bin/awk is mawk but
#     multibyte boundaries matter only for the ellipsis which is appended as
#     a literal string, not counted in the input cap of 1497).

set -euo pipefail

TAG_NAME="${1:?tag_name argument required}"
HTML_URL="${2:?html_url argument required}"

CAP=1500  # readability soft-cap

# Read all of stdin into a variable.
BODY="$(cat)"

# ---------------------------------------------------------------------------
# Step 1 — Extract the Highlights section using awk.
#
# Match any line that starts with "### " and then (optionally) the 📣 emoji
# followed by the word "highlights" (case-insensitive).
#
# Implementation: awk POSIX character classes don't cover Unicode so we use
# a case-folded literal match on the lowercased version of the line for the
# heading test, with a fixed prefix test on the original for efficiency.
# ---------------------------------------------------------------------------
HIGHLIGHTS="$(printf '%s\n' "$BODY" | awk '
  function matches_heading(line,    lower) {
    # Must start with "### " (h3 — Keep-a-Changelog convention)
    if (substr(line, 1, 4) != "### ") return 0
    # Rest of line after "### "
    rest = substr(line, 5)
    # Strip optional leading emoji sequence (non-ASCII chars before a space)
    # by lowercasing ASCII portion — use tolower on the whole rest
    lower = tolower(rest)
    # Accept if the lowercased rest starts with "highlights" optionally
    # preceded by a non-ASCII byte sequence and a space (the emoji + space).
    # Strategy: strip a leading run of non-ASCII bytes followed by a space.
    gsub(/^[^\x01-\x7F]+ /, "", lower)
    return (substr(lower, 1, 10) == "highlights")
  }

  matches_heading($0)   { found=1; next }
  found && /^## |^### /  { exit }
  found                 { lines[++n] = $0 }

  END {
    # Find first non-blank line
    first=1
    while (first <= n && lines[first] ~ /^[[:space:]]*$/) first++
    # Find last non-blank line
    last=n
    while (last >= first && lines[last] ~ /^[[:space:]]*$/) last--
    for (i=first; i<=last; i++) print lines[i]
  }
')"

# ---------------------------------------------------------------------------
# Step 2 — Fallback: if no Highlights section found, emit a minimal description.
# ---------------------------------------------------------------------------
if [ -z "$HIGHLIGHTS" ]; then
  printf '%s published. View notes: %s\n' "$TAG_NAME" "$HTML_URL"
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 3 — Length cap at 1500 chars using awk.
#
# awk's length() counts bytes in most POSIX awks; on ubuntu-latest (mawk)
# this means a multi-byte emoji counts as 3-4 "chars".  For the purposes of
# the 1500-char cap (which is a readability guideline, not a hard limit) this
# is acceptable — the Discord API's own 4096 hard cap is far above 1500.
# The ellipsis appended on truncation is a literal "…" (U+2026, 3 bytes).
#
# We use bash ${#var} which counts characters (not bytes) in a UTF-8 locale.
# ---------------------------------------------------------------------------
CHAR_COUNT=${#HIGHLIGHTS}

if [ "$CHAR_COUNT" -le "$CAP" ]; then
  printf '%s\n' "$HIGHLIGHTS"
else
  # Truncate to 1497 characters + ellipsis, then append the link on a new line.
  TRUNCATED="${HIGHLIGHTS:0:1497}"
  printf '%s…\n' "$TRUNCATED"
  printf 'View full release notes: %s\n' "$HTML_URL"
fi
