#!/usr/bin/env bash
# ============================================================
# release.sh — Publish a GitHub Release for Hailo Voice Assistant.
#
# What it does:
#   1. Reads VERSION
#   2. Validates git tree is clean and CHANGELOG has an entry
#   3. Creates an annotated git tag  v{VERSION}  and pushes it
#      → triggers GitHub Actions build-and-push.yml automatically
#   4. Creates a GitHub Release via gh CLI with the CHANGELOG
#      section as release notes
#
# Prerequisites:
#   - gh CLI installed and authenticated  (gh auth login)
#   - VERSION bumped                      (bash bump-version.sh patch|minor|major)
#   - CHANGELOG.md has a [X.Y.Z] section  (see bump-version.sh output for template)
#   - VERSION + CHANGELOG committed       (git add VERSION CHANGELOG.md && git commit ...)
#   - main branch pushed to origin        (git push origin main)
#
# Usage:
#   bash release.sh              # full release
#   bash release.sh --dry-run    # print what would happen, no side effects
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION_FILE="$SCRIPT_DIR/VERSION"
CHANGELOG="$SCRIPT_DIR/CHANGELOG.md"

# ── Flags ─────────────────────────────────────────────────────────────────────
DRY_RUN=0
for arg in "$@"; do
    [[ "$arg" == "--dry-run" ]] && DRY_RUN=1
done

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✔  $*${NC}"; }
info() { echo -e "${CYAN}  →  $*${NC}"; }
warn() { echo -e "${YELLOW}  ⚠  $*${NC}"; }
die()  { echo -e "${RED}  ✘  $*${NC}"; exit 1; }
dry()  { echo -e "${YELLOW}  [DRY-RUN]  $*${NC}"; }

# ═══════════════════════════════════════════════════════════════
# Pre-flight
# ═══════════════════════════════════════════════════════════════
command -v gh &>/dev/null \
    || die "gh CLI not found. Install it: https://cli.github.com"

if [[ $DRY_RUN -eq 0 ]]; then
    gh auth status &>/dev/null \
        || die "gh CLI not authenticated. Run: gh auth login"
fi

# ── Read version ──────────────────────────────────────────────────────────────
[[ -f "$VERSION_FILE" ]] || die "VERSION file not found. Is this the right directory?"
VERSION=$(cat "$VERSION_FILE" | tr -d '[:space:]')
[[ -n "$VERSION" ]] || die "VERSION file is empty. Run bump-version.sh first."
TAG="v${VERSION}"

echo ""
echo -e "${CYAN}╔═══════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   Hailo Voice Assistant — Release ${TAG}${NC}"
echo -e "${CYAN}╚═══════════════════════════════════════════════════════╝${NC}"
echo ""
[[ $DRY_RUN -eq 1 ]] && warn "DRY-RUN mode — no changes will be made"
echo ""

# ═══════════════════════════════════════════════════════════════
# STEP 1 — Check git working tree
# ═══════════════════════════════════════════════════════════════
info "Checking git working tree..."
DIRTY=0
git -C "$SCRIPT_DIR" diff --quiet          || DIRTY=1
git -C "$SCRIPT_DIR" diff --cached --quiet || DIRTY=1

if [[ $DIRTY -eq 1 ]]; then
    warn "Uncommitted changes detected:"
    git -C "$SCRIPT_DIR" status --short
    echo ""
    read -r -p "  Continue with uncommitted changes? [y/N] " CONFIRM
    [[ "$CONFIRM" =~ ^[Yy]$ ]] || { echo "  Aborted. Commit changes first."; exit 1; }
else
    ok "Git tree: clean"
fi

# ═══════════════════════════════════════════════════════════════
# STEP 2 — Extract CHANGELOG entry for this version
# ═══════════════════════════════════════════════════════════════
info "Extracting CHANGELOG.md entry for [${VERSION}]..."

# Pull every line between "## [X.Y.Z]" and the next "## [" heading,
# collapsing consecutive blank lines into one (works on BSD + GNU awk).
RELEASE_NOTES=$(awk \
    "/^## \[${VERSION}\]/{found=1; next}
     found && /^## \[/{exit}
     found{
       if (/^[[:space:]]*$/) { blank++; next }
       if (blank > 0) { print \"\"; blank=0 }
       print
     }" \
    "$CHANGELOG")

if [[ -z "$RELEASE_NOTES" ]]; then
    warn "No content found under '## [${VERSION}]' in CHANGELOG.md."
    read -r -p "  Continue with empty release notes? [y/N] " CONFIRM
    [[ "$CONFIRM" =~ ^[Yy]$ ]] \
        || { echo "  Aborted. Fill in the [${VERSION}] section of CHANGELOG.md first."; exit 1; }
    RELEASE_NOTES="See [CHANGELOG.md](CHANGELOG.md) for details."
else
    ok "Release notes ready ($(echo "$RELEASE_NOTES" | wc -l | tr -d ' ') lines)"
fi

# ═══════════════════════════════════════════════════════════════
# STEP 3 — Create and push annotated git tag
# ═══════════════════════════════════════════════════════════════
info "Creating git tag ${TAG}..."
if git -C "$SCRIPT_DIR" tag -l "$TAG" | grep -q "$TAG"; then
    die "Tag ${TAG} already exists. Did you forget to bump the version? (bash bump-version.sh patch)"
fi

if [[ $DRY_RUN -eq 0 ]]; then
    git -C "$SCRIPT_DIR" tag -a "$TAG" -m "Release ${TAG}"
    ok "Tag created: ${TAG}"
else
    dry "Would run: git tag -a ${TAG} -m 'Release ${TAG}'"
fi

info "Pushing tag ${TAG} to origin..."
if [[ $DRY_RUN -eq 0 ]]; then
    git -C "$SCRIPT_DIR" push origin "$TAG"
    ok "Tag pushed → GitHub Actions build triggered"
else
    dry "Would run: git push origin ${TAG}"
    dry "Trigger: build-and-push.yml (build hailo-whisper + hailo-ollama images)"
fi

# ═══════════════════════════════════════════════════════════════
# STEP 4 — Create GitHub Release
# ═══════════════════════════════════════════════════════════════
info "Creating GitHub Release ${TAG}..."
if [[ $DRY_RUN -eq 0 ]]; then
    RELEASE_URL=$(gh release create "$TAG" \
        --title "${TAG}" \
        --notes "${RELEASE_NOTES}")
    ok "GitHub Release created: ${RELEASE_URL}"
else
    dry "Would run: gh release create ${TAG} --title '${TAG}' --notes '...'"
    echo ""
    echo "  Release notes preview:"
    echo "  ─────────────────────────────────────────────────"
    echo "$RELEASE_NOTES"
    echo "  ─────────────────────────────────────────────────"
    RELEASE_URL="<dry-run>"
fi

# ═══════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════
echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   Release ${TAG} published!                            ║${NC}"
echo -e "${GREEN}╠═══════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║  GitHub Release : ${RELEASE_URL}${NC}"
echo -e "${GREEN}║  CI build       : building images now...              ║${NC}"
echo -e "${GREEN}╠═══════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║  Watch the build:                                     ║${NC}"
echo -e "${GREEN}║    gh run watch                                       ║${NC}"
echo -e "${GREEN}╠═══════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║  Deploy to Pi once images are pushed:                 ║${NC}"
echo -e "${GREEN}║    ssh rpi.local '                                    ║${NC}"
echo -e "${GREEN}║      cd ~/homeassistant &&                            ║${NC}"
echo -e "${GREEN}║      docker compose pull hailo-whisper hailo-ollama &&║${NC}"
echo -e "${GREEN}║      docker compose up -d --force-recreate            ║${NC}"
echo -e "${GREEN}║        hailo-whisper hailo-ollama'                    ║${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════════╝${NC}"
echo ""
