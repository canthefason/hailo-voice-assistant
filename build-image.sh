#!/usr/bin/env bash
# ============================================================
# build-image.sh — Build the hailo-whisper Docker image on the Pi
#                  and optionally push to a registry.
#
# All inputs come from environment variables so this script can be
# called from CI (GitHub Actions self-hosted runner on the Pi) or
# directly from your Mac.
#
# Usage (from your Mac):
#   bash build-image.sh             # build latest, no push
#   bash build-image.sh --push      # build and push to Docker Hub
#
# Override defaults with environment variables:
#   RPI_HOST          — SSH alias / hostname      (default: rpi.local)
#   RPI_USER          — Pi username               (default: pi)
#   IMAGE_NAME        — Docker image name         (default: canthefason/wyoming-hailo-whisper)
#   IMAGE_TAG         — Docker image tag          (default: value in VERSION file)
#   WHISPER_MODEL     — Whisper decoder model     (default: small.en)
#   HEF               — HEF filename on the Pi   (default: auto-detect)
#   PUSH_IMAGE        — Push after build? (1/0)   (default: 0, or 1 with --push)
#
# Note: The HailoRT wheel is NOT needed for the build. hailo_platform is
# bind-mounted from the Pi host at runtime — it is never baked into the image.
# ============================================================
set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
SSH_ALIAS="${RPI_HOST:-rpi.local}"
RPI_USER="${RPI_USER:-pi}"
IMAGE_NAME="${IMAGE_NAME:-canthefason/wyoming-hailo-whisper}"
_VERSION_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/VERSION"
IMAGE_TAG="${IMAGE_TAG:-$(cat "$_VERSION_FILE" 2>/dev/null | tr -d '[:space:]' || echo 'latest')}"
WHISPER_MODEL="${WHISPER_MODEL:-small.en}"
PUSH_IMAGE="${PUSH_IMAGE:-0}"

# Optional: allow passing --push as a positional arg
for arg in "$@"; do
    [[ "$arg" == "--push" ]] && PUSH_IMAGE=1
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${SCRIPT_DIR}/build-image.log"
exec > >(tee "${LOG_FILE}") 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') — build-image.sh started ==="
echo ""

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
ok()     { echo -e "${GREEN}  ✔  $*${NC}"; }
info()   { echo -e "${CYAN}  →  $*${NC}"; }
header() { echo -e "\n${BOLD}${CYAN}━━━  $*  ━━━${NC}\n"; }
warn()   { echo -e "${YELLOW}  ⚠  $*${NC}"; }
die()    { echo -e "${RED}  ✘  $*${NC}"; echo "=== FAILED ===" ; exit 1; }

pi() { ssh "${RPI_USER}@${SSH_ALIAS}" "bash -l -s" <<< "$@"; }

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}╔═══════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   hailo-whisper — Docker Image Build                  ║${NC}"
echo -e "${CYAN}╠═══════════════════════════════════════════════════════╣${NC}"
echo -e "${CYAN}║  Image  : ${BOLD}${IMAGE_NAME}:${IMAGE_TAG}${NC}"
echo -e "${CYAN}║  Model  : ${BOLD}${WHISPER_MODEL}${NC}"
echo -e "${CYAN}║  Push   : ${BOLD}$( [[ $PUSH_IMAGE -eq 1 ]] && echo yes || echo no )${NC}"
echo -e "${CYAN}╚═══════════════════════════════════════════════════════╝${NC}"
echo ""

# ═══════════════════════════════════════════════════════════════
# STEP 1 — Pre-flight
# ═══════════════════════════════════════════════════════════════
header "STEP 1 — Pre-flight"

info "Testing SSH connection to ${SSH_ALIAS}..."
pi "echo SSH_OK" | grep -q SSH_OK \
    || die "Cannot SSH to ${SSH_ALIAS}. Run 00-setup-ssh.sh first."
ok "SSH: connected"

# ═══════════════════════════════════════════════════════════════
# STEP 2 — Locate encoder HEF on Pi
# ═══════════════════════════════════════════════════════════════
header "STEP 2 — Locate encoder HEF on Pi"

# Map model name to expected HEF filename; user can override with HEF=...
if [[ -n "${HEF:-}" ]]; then
    HEF_FILENAME="${HEF}"
else
    # Derive HEF filename from model name: small.en → whisper_small_en_encoder.hef
    MODEL_SLUG=$(echo "${WHISPER_MODEL}" | tr '.' '_')
    HEF_FILENAME="whisper_${MODEL_SLUG}_encoder.hef"
fi
info "Looking for HEF '${HEF_FILENAME}' on Pi..."
HEF_PATH=$(pi "
    find \$HOME /opt /usr/local/share \
        -name '${HEF_FILENAME}' \
        2>/dev/null | head -1 || true
") || true
[[ -n "${HEF_PATH}" ]] || die "HEF '${HEF_FILENAME}' not found on Pi. Download it from the Hailo CDN or set HEF= to an explicit path."
ok "HEF: ${HEF_PATH}"

# ═══════════════════════════════════════════════════════════════
# STEP 3 — Assemble build context on Pi
# ═══════════════════════════════════════════════════════════════
header "STEP 3 — Assemble build context on Pi"

BUILD_DIR="/home/${RPI_USER}/hailo-whisper-build"
info "Preparing build context at ${BUILD_DIR}..."
pi "
    set -e
    rm -rf ${BUILD_DIR}
    mkdir -p ${BUILD_DIR}

    cp '${HEF_PATH}' ${BUILD_DIR}/${HEF_FILENAME}
    echo '  HEF staged: ${HEF_FILENAME}'
    echo 'CONTEXT_READY'
" | grep -E 'CONTEXT_READY|staged'
ok "Build context ready"

# Copy Dockerfile and source from Mac → Pi build context
info "Uploading Dockerfile and wyoming source..."
scp -r "${SCRIPT_DIR}/Dockerfile" \
        "${SCRIPT_DIR}/wyoming_hailo_whisper/" \
        "${RPI_USER}@${SSH_ALIAS}:${BUILD_DIR}/"
ok "Source uploaded"

# ═══════════════════════════════════════════════════════════════
# STEP 4 — docker build on Pi
# ═══════════════════════════════════════════════════════════════
header "STEP 4 — docker build"

info "Building ${IMAGE_NAME}:${IMAGE_TAG}..."
info "  HEF          = ${HEF_FILENAME}"
info "  WHISPER_MODEL= ${WHISPER_MODEL}"
echo ""

BUILD_RC=0
pi "
    set -e
    cd ${BUILD_DIR}
    docker build \
        --build-arg HEF=${HEF_FILENAME} \
        --build-arg WHISPER_MODEL=${WHISPER_MODEL} \
        -t ${IMAGE_NAME}:${IMAGE_TAG} \
        . 2>&1
    echo BUILD_DONE
" | tee /dev/stderr | grep -q BUILD_DONE || BUILD_RC=$?
[[ $BUILD_RC -eq 0 ]] || die "docker build failed. Check output above."
ok "Image built: ${IMAGE_NAME}:${IMAGE_TAG}"

# Stamp the short commit SHA as an additional tag if we are in a git repo
GIT_SHA=$(git -C "${SCRIPT_DIR}" rev-parse --short HEAD 2>/dev/null || true)
if [[ -n "${GIT_SHA}" ]]; then
    pi "docker tag ${IMAGE_NAME}:${IMAGE_TAG} ${IMAGE_NAME}:${GIT_SHA}" || true
    ok "Also tagged: ${IMAGE_NAME}:${GIT_SHA}"
fi

# ═══════════════════════════════════════════════════════════════
# STEP 5 — Push (optional)
# ═══════════════════════════════════════════════════════════════
if [[ "${PUSH_IMAGE}" -eq 1 ]]; then
    header "STEP 5 — docker push"
    info "Pushing ${IMAGE_NAME}:${IMAGE_TAG}..."
    pi "
        docker push ${IMAGE_NAME}:${IMAGE_TAG}
        echo PUSH_DONE
    " | grep PUSH_DONE || die "docker push failed"
    [[ -n "${GIT_SHA}" ]] && pi "docker push ${IMAGE_NAME}:${GIT_SHA}" || true
    ok "Pushed: ${IMAGE_NAME}:${IMAGE_TAG}"
else
    header "STEP 5 — Push skipped"
    info "Re-run with --push (or set PUSH_IMAGE=1) to push to registry."
fi

# ═══════════════════════════════════════════════════════════════
# STEP 6 — Smoke test
# ═══════════════════════════════════════════════════════════════
header "STEP 6 — Smoke test"

info "Verifying image layers and entrypoint..."
pi "
    docker inspect ${IMAGE_NAME}:${IMAGE_TAG} \
        --format 'Entrypoint: {{json .Config.Entrypoint}}  Cmd: {{json .Config.Cmd}}'
    docker history ${IMAGE_NAME}:${IMAGE_TAG} --no-trunc --format '{{.CreatedBy}}' | grep -i whisper | head -3 || true
    echo SMOKE_OK
" | grep SMOKE_OK
ok "Image looks healthy"

echo ""
echo -e "${GREEN}╔═══════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   Build complete!                                      ║${NC}"
echo -e "${GREEN}╠═══════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║  Image : ${IMAGE_NAME}:${IMAGE_TAG}${NC}"
echo -e "${GREEN}║  Model : ${WHISPER_MODEL}${NC}"
echo -e "${GREEN}╚═══════════════════════════════════════════════════════╝${NC}"
echo ""
echo "  To deploy immediately:"
echo "  ssh ${SSH_ALIAS} 'cd \$(echo \${HA_DIR:-~/homeassistant}) && docker compose up -d --force-recreate hailo-whisper'"
echo ""
echo "=== $(date '+%Y-%m-%d %H:%M:%S') — completed ==="
echo "Log saved to: ${LOG_FILE}"
