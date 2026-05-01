#!/usr/bin/env bash
# ============================================================
# bump-version.sh — Increment the project version in VERSION.
#
# Usage:
#   bash bump-version.sh patch    # 1.0.0 → 1.0.1  (bug fixes)
#   bash bump-version.sh minor    # 1.0.0 → 1.1.0  (new features, backward-compatible)
#   bash bump-version.sh major    # 1.0.0 → 2.0.0  (breaking changes)
#
# After bumping:
#   1. Add a [X.Y.Z] section to CHANGELOG.md (under [Unreleased])
#   2. Commit: git add VERSION CHANGELOG.md && git commit -m "chore: release vX.Y.Z"
#   3. Release: bash release.sh
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION_FILE="$SCRIPT_DIR/VERSION"
CHANGELOG="$SCRIPT_DIR/CHANGELOG.md"

# ── Parse argument ────────────────────────────────────────────────────────────
BUMP="${1:-patch}"
if [[ ! "$BUMP" =~ ^(major|minor|patch)$ ]]; then
    echo "Usage: $0 [major|minor|patch]"
    echo ""
    echo "  patch  — bug fixes, no new features   (1.0.0 → 1.0.1)"
    echo "  minor  — new features, backward-compat (1.0.0 → 1.1.0)"
    echo "  major  — breaking changes              (1.0.0 → 2.0.0)"
    exit 1
fi

# ── Read current version ──────────────────────────────────────────────────────
[[ -f "$VERSION_FILE" ]] || { echo "✘  VERSION file not found at $VERSION_FILE"; exit 1; }
CURRENT=$(cat "$VERSION_FILE" | tr -d '[:space:]')
IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT"

# ── Compute new version ───────────────────────────────────────────────────────
case "$BUMP" in
    major) MAJOR=$((MAJOR + 1)); MINOR=0; PATCH=0 ;;
    minor) MINOR=$((MINOR + 1)); PATCH=0 ;;
    patch) PATCH=$((PATCH + 1)) ;;
esac
NEW="${MAJOR}.${MINOR}.${PATCH}"

# ── Write back ────────────────────────────────────────────────────────────────
echo "$NEW" > "$VERSION_FILE"

echo ""
echo "  ✔  Version bumped: $CURRENT → $NEW"
echo ""

# ── Print CHANGELOG hint ──────────────────────────────────────────────────────
TODAY=$(date +%Y-%m-%d)
echo "  Add this section to CHANGELOG.md (replace [Unreleased] content):"
echo ""
echo "  ─────────────────────────────────────────────────────────────"
echo "  ## [$NEW] — $TODAY"
echo ""
echo "  ### Added"
echo "  - "
echo ""
echo "  ### Changed"
echo "  - "
echo ""
echo "  ### Fixed"
echo "  - "
echo "  ─────────────────────────────────────────────────────────────"
echo ""
echo "  Next steps:"
echo "    1. Edit CHANGELOG.md — fill in the [$NEW] section above"
echo "    2. git add VERSION CHANGELOG.md"
echo "    3. git commit -m \"chore: release v${NEW}\""
echo "    4. bash release.sh"
echo ""
