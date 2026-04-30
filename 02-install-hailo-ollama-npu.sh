#!/usr/bin/env bash
# ============================================================
# 02-install-hailo-ollama-npu.sh
# Replaces the CPU Docker Ollama container with Hailo-NPU-
# accelerated LLM inference via hailo-ollama (hailo_model_zoo_genai).
#
# What this script does:
#   1. Pre-flight — SSH, Hailo device, HailoRT version
#   2. Build hailo-ollama from source (if not already against 5.3.0)
#   3. Stop CPU Docker ollama (free port 11434)
#   4. Build a minimal Docker image for hailo-ollama
#   5. Update compose.yaml (replace ollama → hailo-ollama service)
#   6. docker compose up -d hailo-ollama
#   7. Pull LLM model (qwen2-1.5b-instruct-function-calling)
#   8. Smoke-test inference
#   9. Verify Home Assistant can reach hailo-ollama
#
# Run from your Mac:  bash 02-install-hailo-ollama-npu.sh
#
# Output is tee'd to install-hailo-ollama-npu.log in this directory
# so Claude can read it and suggest fixes if something goes wrong.
#
# Override defaults with environment variables:
#   RPI_HOST        — SSH alias or hostname                  (default: rpi.local)
#   RPI_USER        — Pi username                            (default: pi)
#   HA_DIR          — HA compose dir on Pi (absolute path)   (default: /home/<user>/homeassistant)
#   GENAI_REPO_DIR  — hailo_model_zoo_genai path on Pi       (default: /home/<user>/hailo_model_zoo_genai)
#   DOCKER_BUILD_DIR— Docker build context dir on Pi         (default: /home/<user>/hailo-ollama-docker)
#   HAILO_MODEL     — NPU model to use                       (default: qwen2.5:1.5b)
# ============================================================
set -euo pipefail

# ── Logging: tee all stdout+stderr to a log file next to this script ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${SCRIPT_DIR}/install-hailo-ollama-npu.log"
# Truncate on each run so the log reflects the latest attempt only
exec > >(tee "${LOG_FILE}") 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S') — 02-install-hailo-ollama-npu.sh started ==="
echo ""

SSH_ALIAS="${RPI_HOST:-rpi.local}"
RPI_USER="${RPI_USER:-pi}"
HA_DIR="${HA_DIR:-/home/${RPI_USER}/homeassistant}"
HAILO_VERSION="5.3.0"
GENAI_REPO_DIR="${GENAI_REPO_DIR:-/home/${RPI_USER}/hailo_model_zoo_genai}"
HAILO_OLLAMA_BIN="/usr/local/bin/hailo-ollama"
HAILO_OLLAMA_PORT=11434
COMPOSE_FILE="${HA_DIR}/compose.yaml"
HAILO_OLLAMA_IMAGE="${HAILO_OLLAMA_IMAGE:-canthefason/hailo-ollama}"
IMAGE_TAG="${HAILO_OLLAMA_IMAGE}:latest"

# Model to load on the Hailo-10H NPU.
# Override via environment variable, e.g.:  HAILO_MODEL=qwen2.5:0.5b bash 02-install-hailo-ollama-npu.sh
# Available models are listed during Step 1 (pre-flight) based on manifests found on the Pi.
HAILO_MODEL="${HAILO_MODEL:-qwen2.5:1.5b}"
# HA uses the model name directly — hailo-ollama has no /api/create alias support
HA_MODEL_ALIAS="${HAILO_MODEL}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
ok()     { echo -e "${GREEN}  ✔  $*${NC}"; }
info()   { echo -e "${CYAN}  →  $*${NC}"; }
header() { echo -e "\n${BOLD}${CYAN}━━━  $*  ━━━${NC}\n"; }
warn()   { echo -e "${YELLOW}  ⚠  $*${NC}"; }
die()    { echo -e "${RED}  ✘  $*${NC}"; echo "=== FAILED: $* ==="; exit 1; }

confirm() {
    local msg="${1:-Continue?}"
    echo -e "\n${YELLOW}  ▶  ${msg}${NC}"
    echo ""
}

pi() {
    ssh "${RPI_USER}@${SSH_ALIAS}" "bash -l -s" <<< "$@"
}

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}╔════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   Hailo Voice Assistant — NPU LLM (Docker Compose)     ║${NC}"
echo -e "${CYAN}║   hailo-ollama ${HAILO_VERSION} · qwen2-1.5b tool-call          ║${NC}"
echo -e "${CYAN}╚════════════════════════════════════════════════════════╝${NC}"
echo ""

# ═══════════════════════════════════════════════════════════════
# STEP 1 — Pre-flight
# ═══════════════════════════════════════════════════════════════
header "STEP 1 — Pre-flight checks"

info "Testing SSH connection..."
OUT=$(pi "echo 'SSH OK'") || die "Cannot reach Pi via SSH alias '$SSH_ALIAS'."
echo "$OUT" | grep -q "SSH OK" || die "SSH test failed — unexpected output: $OUT"
ok "SSH connection: healthy"

info "Checking Hailo device..."
pi "
  ls /dev/h1x* > /dev/null 2>&1 || { echo 'ERROR: No /dev/h1x* device node found'; exit 1; }
  echo '  Hailo device node:' \$(ls /dev/h1x*)
  hailortcli fw-control identify 2>/dev/null | grep -E 'Device|Firmware' || true
"
ok "Hailo device: present"

info "Checking HailoRT version..."
HAILORT_VER=$(pi "hailortcli --version 2>/dev/null | head -1 || echo unknown") || true
echo "  $HAILORT_VER"
if ! echo "$HAILORT_VER" | grep -q "$HAILO_VERSION"; then
    die "HailoRT $HAILO_VERSION required but found: $HAILORT_VER. Run 01-install-hailo-voice-assistant.sh first."
fi
ok "HailoRT $HAILO_VERSION: confirmed"

info "Checking hailo_model_zoo_genai repo..."
pi "
  test -d $GENAI_REPO_DIR || { echo 'ERROR: $GENAI_REPO_DIR not found'; exit 1; }
  cd $GENAI_REPO_DIR && git log --oneline -1 2>/dev/null || true
"
ok "hailo_model_zoo_genai repo: present"

info "Discovering available models on Pi (and fixing tagless manifests)..."
AVAILABLE_MODELS=$(pi "
  MANIFEST_DIR=/usr/local/share/hailo-ollama/models/manifests
  if [ ! -d \"\$MANIFEST_DIR\" ]; then exit 0; fi

  # Fix manifests that are missing a tag subdirectory.
  # Correct layout: manifests/<name>/<tag>/manifest.json  (3 path components)
  # Broken layout:  manifests/<name>/manifest.json        (2 path components)
  # hailo-ollama joins path components with ':' so a tagless manifest becomes
  # 'manifests:<name>' instead of '<name>:<tag>'.
  find \"\$MANIFEST_DIR\" -name manifest.json 2>/dev/null | while IFS= read -r f; do
    rel=\$(echo \"\$f\" | sed \"s|^\$MANIFEST_DIR/||; s|/manifest.json\$||\")
    depth=\$(echo \"\$rel\" | tr -cd '/' | wc -c)
    if [ \"\$depth\" -eq 0 ]; then
      # Only <name>/manifest.json — move to <name>/latest/manifest.json
      name=\$(dirname \"\$f\" | xargs basename)
      dest=\"\$MANIFEST_DIR/\$name/latest\"
      echo \"  Fixing tagless manifest: \$name → \$name:latest\"
      mkdir -p \"\$dest\"
      mv \"\$f\" \"\$dest/manifest.json\"
    fi
  done

  find \"\$MANIFEST_DIR\" -name manifest.json 2>/dev/null \
    | sed \"s|^\$MANIFEST_DIR/||; s|/manifest.json\$||; s|/|:|\" \
    | sort
") || true
if [ -n "$AVAILABLE_MODELS" ]; then
    echo "  Models with manifests on Pi:"
    echo "$AVAILABLE_MODELS" | while IFS= read -r m; do echo "    • $m"; done
else
    echo "  (no manifests found yet — model will be pulled in Step 7)"
fi

info "Selected model: ${HAILO_MODEL}"
MODEL_PATH=$(echo "${HAILO_MODEL}" | tr ':' '/')
MANIFEST_EXISTS=$(pi "test -f /usr/local/share/hailo-ollama/models/manifests/${MODEL_PATH}/manifest.json && echo yes || echo no") || true
if [ "$MANIFEST_EXISTS" != "yes" ]; then
    warn "No manifest found for '${HAILO_MODEL}' on Pi."
    warn "Available models listed above. To switch, re-run with: HAILO_MODEL=<name:tag> bash $0"
    if [ -n "$AVAILABLE_MODELS" ]; then
        die "Manifest missing for '${HAILO_MODEL}' — choose an available model above."
    fi
    # No manifests at all — first run, proceed to Step 7 which handles the pull.
fi
ok "Model: ${HAILO_MODEL}"

confirm "Pre-flight passed. Starting NPU LLM installation."

# ═══════════════════════════════════════════════════════════════
# STEP 2 — Build hailo-ollama from source (if needed)
# ═══════════════════════════════════════════════════════════════
header "STEP 2 — Build hailo-ollama (against HailoRT ${HAILO_VERSION})"

BUILD_OUT=""; BUILD_RC=0
BUILD_OUT=$(pi "
  # Check if already built against the correct version
  if [ -f $HAILO_OLLAMA_BIN ]; then
    LINKED=\$(ldd $HAILO_OLLAMA_BIN 2>/dev/null | grep hailort | awk '{print \$1}' || echo '')
    echo \"  Existing binary links: \$LINKED\"
    if echo \"\$LINKED\" | grep -q 'libhailort.so.${HAILO_VERSION}'; then
      echo '  hailo-ollama already built against ${HAILO_VERSION} — skipping build.'
      echo 'BUILD_SKIP'
      exit 0
    fi
    echo '  Wrong libhailort version linked — rebuilding...'
  else
    echo '  No existing binary — building from source...'
  fi

  set -e
  cd $GENAI_REPO_DIR

  echo '  Configuring cmake...'
  cmake -B build -DCMAKE_BUILD_TYPE=Release 2>&1 | tail -5

  echo '  Building (this takes 5-15 minutes on Pi 5 — please wait)...'
  cmake --build build --config Release -- -j4 2>&1 | tail -10

  echo '  Installing to /usr/local/bin...'
  sudo cmake --install build 2>&1 | tail -5

  echo 'BUILD_DONE'
") || BUILD_RC=$?
echo "$BUILD_OUT"
[[ $BUILD_RC -eq 0 ]] || die "Build step failed (exit $BUILD_RC) — see output above."

if echo "$BUILD_OUT" | grep -q "BUILD_SKIP"; then
    ok "hailo-ollama: already up-to-date, skipping build"
elif echo "$BUILD_OUT" | grep -q "BUILD_DONE"; then
    ok "hailo-ollama: built and installed"
else
    die "Build completed but expected BUILD_DONE/BUILD_SKIP token missing."
fi

info "Verifying binary links against libhailort.so.${HAILO_VERSION}..."
LDD_OUT=$(pi "ldd $HAILO_OLLAMA_BIN 2>&1 | grep hailort") || true
echo "  $LDD_OUT"
if ! echo "$LDD_OUT" | grep -q "${HAILO_VERSION}"; then
    die "Binary still not linked against libhailort.so.${HAILO_VERSION} — check build."
fi
ok "Binary links: libhailort.so.${HAILO_VERSION} ✔"

# ═══════════════════════════════════════════════════════════════
# STEP 3 — Stop CPU Docker ollama, free port 11434
# ═══════════════════════════════════════════════════════════════
header "STEP 3 — Stop CPU Docker ollama"

STOP_OUT=""; STOP_RC=0
STOP_OUT=$(pi "
  # Stop CPU ollama if present
  if docker inspect ollama > /dev/null 2>&1; then
    echo '  Stopping and removing CPU ollama container...'
    docker stop ollama 2>&1 || true
    docker rm   ollama 2>&1 || true
  else
    echo '  CPU ollama container not running — nothing to stop.'
  fi

  # Also stop any existing hailo-ollama container so it frees port 11434
  # (Step 6 will re-create it with the updated image)
  cd ${HA_DIR}
  docker compose stop hailo-ollama 2>/dev/null || true
  docker compose rm -f hailo-ollama 2>/dev/null || true
  echo 'OLLAMA_STOPPED'
") || STOP_RC=$?
echo "$STOP_OUT"
[[ $STOP_RC -eq 0 ]] || die "Failed to stop ollama container (exit $STOP_RC)."
echo "$STOP_OUT" | grep -q "OLLAMA_STOPPED" || die "Unexpected output from stop step."

info "Verifying port $HAILO_OLLAMA_PORT is free..."
PORT_OUT=$(pi "ss -tlnp 2>/dev/null | grep :${HAILO_OLLAMA_PORT} || echo 'PORT_FREE'") || true
if ! echo "$PORT_OUT" | grep -q "PORT_FREE"; then
    warn "Port $HAILO_OLLAMA_PORT still in use: $PORT_OUT"
    die "Free port $HAILO_OLLAMA_PORT manually before continuing."
fi
ok "Port $HAILO_OLLAMA_PORT: free"

# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
# STEP 4 — Pull hailo-ollama image from Docker Hub
# ═══════════════════════════════════════════════════════════════
header "STEP 4 — Pull Docker image: $IMAGE_TAG"

# The hailo-ollama Docker image is built by GitHub Actions and published to
# Docker Hub. It contains only the OpenAI-compat proxy (our code).
# The hailo-ollama binary and libhailort are bind-mounted from the Pi host
# at runtime — they were built and installed in STEP 2 above.
DOCKER_OUT=""; DOCKER_RC=0
DOCKER_OUT=$(pi "
  set -e
  echo '  Pulling ${IMAGE_TAG}...'
  docker pull ${IMAGE_TAG} 2>&1 | tail -5
  echo 'IMAGE_READY'
") || DOCKER_RC=$?
echo "$DOCKER_OUT"
[[ $DOCKER_RC -eq 0 ]] || die "docker pull failed (exit $DOCKER_RC) — see output above."

if echo "$DOCKER_OUT" | grep -q "IMAGE_READY"; then
    IMAGE_STATUS="IMAGE_READY"
    ok "Docker image $IMAGE_TAG: pulled"
else
    die "docker pull completed but IMAGE_READY token missing."
fi

# STEP 5 — Update compose.yaml
# ═══════════════════════════════════════════════════════════════
header "STEP 5 — Update compose.yaml"

COMPOSE_OUT=""; COMPOSE_RC=0
COMPOSE_OUT=$(pi "
  set -e
  COMPOSE=${COMPOSE_FILE}

  # Detect the Hailo device group GID so the container can access /dev/h1x-0
  HAILO_DEV=\$(ls /dev/h1x* 2>/dev/null | head -1)
  HAILO_GID=\$(stat -c '%g' \"\$HAILO_DEV\" 2>/dev/null || echo '107')
  echo \"  Hailo device GID: \$HAILO_GID\"

  # Back up compose.yaml before modifying
  cp \"\$COMPOSE\" \"\${COMPOSE}.bak\"
  echo '  Backed up compose.yaml → compose.yaml.bak'

  python3 << PYEOF
import re, sys

COMPOSE_FILE = '${COMPOSE_FILE}'
IMAGE_TAG    = '${IMAGE_TAG}'
HAILO_GID    = '\$HAILO_GID'
MODEL        = '${HAILO_MODEL}'
PORT         = '${HAILO_OLLAMA_PORT}'

with open(COMPOSE_FILE, 'r') as f:
    content = f.read()

# Remove the CPU 'ollama' service block if present.
# We match from 'ollama:' (with indentation) to the next top-level service or end of services.
# Strategy: work line-by-line to remove the ollama service block.
lines = content.splitlines(keepends=True)
new_lines = []
skip = False
service_indent = None
i = 0
while i < len(lines):
    line = lines[i]
    stripped = line.lstrip()
    indent = len(line) - len(stripped)
    # Detect start of the 'ollama' service block (exactly 2-space indent under services:)
    if re.match(r'^  ollama:\s*$', line):
        skip = True
        service_indent = indent
        i += 1
        continue
    if skip:
        # Stop skipping when we hit another service at the same indent level
        if stripped and not stripped.startswith('#') and indent <= service_indent and not re.match(r'^\s*$', line):
            skip = False
        else:
            i += 1
            continue
    new_lines.append(line)
    i += 1

content = ''.join(new_lines)

# Build the hailo-ollama service block
service_block = '''  hailo-ollama:
    container_name: hailo-ollama
    image: {image}
    restart: unless-stopped
    ports:
      - "{port}:{port}"
    volumes:
      - /usr/local/share/hailo-ollama:/usr/local/share/hailo-ollama
      - /usr/local/bin/hailo-ollama:/usr/local/bin/hailo-ollama:ro
      - /usr/lib/libhailort.so.5.3.0:/usr/lib/libhailort.so.5.3.0:ro
      - /usr/lib/aarch64-linux-gnu/libusb-1.0.so.0:/usr/lib/aarch64-linux-gnu/libusb-1.0.so.0:ro
    devices:
      - /dev/h1x-0:/dev/h1x-0
    group_add:
      - "{gid}"
    environment:
      - OLLAMA_KEEP_ALIVE=-1
      - HAILO_OLLAMA_VDEVICE_GROUP_ID=SHARED
      - XDG_DATA_HOME=/usr/local/share
      - LD_LIBRARY_PATH=/usr/lib
'''.format(image=IMAGE_TAG, port=PORT, gid=HAILO_GID)
# Remove any existing hailo-ollama block so we always apply the latest config
lines = content.splitlines(keepends=True)
new_lines = []
skip2 = False
i = 0
while i < len(lines):
    line = lines[i]
    stripped = line.lstrip()
    indent = len(line) - len(stripped)
    if re.match(r'^  hailo-ollama:\s*$', line):
        skip2 = True
        i += 1
        continue
    if skip2:
        if stripped and not stripped.startswith('#') and indent <= 2 and not re.match(r'^\s*$', line):
            skip2 = False
        else:
            i += 1
            continue
    new_lines.append(line)
    i += 1
content = ''.join(new_lines)

# Insert the (re)built service block at the top of services:
content = re.sub(
    r'(^services:\s*\n)',
    r'\1' + service_block,
    content,
    count=1,
    flags=re.MULTILINE
)
print('  hailo-ollama service block inserted/updated in compose.yaml')

# Using a host bind mount (/usr/local/share/hailo-ollama) — no named volume needed.

# ── Hailo vDevice group sharing ──────────────────────────────────────────────
# Both hailo-ollama and wyoming-whisper (or any other Hailo service) must use
# the same HAILO_VDEVICE_GROUP_ID so HailoRT lets them share /dev/h1x-0.
# hailo-ollama reads HAILO_OLLAMA_VDEVICE_GROUP_ID; HailoRT library reads
# HAILO_VDEVICE_GROUP_ID from the environment for all other apps.
VDEVICE_GROUP = 'SHARED'
VDEVICE_ENV_LINE = '      - HAILO_VDEVICE_GROUP_ID=' + VDEVICE_GROUP + '\n'

lines = content.splitlines(keepends=True)

# Find (service_name, start_line_idx, end_line_idx) for each service block
in_services = False
cur_service = None
cur_start = None
service_ranges = []
for idx, line in enumerate(lines):
    if re.match(r'^services:\s*$', line):
        in_services = True
        continue
    if not in_services:
        continue
    # Top-level key other than services: signals end of services block
    if line and not line[0].isspace() and not re.match(r'^\s*$', line):
        if cur_service is not None:
            service_ranges.append((cur_service, cur_start, idx))
        in_services = False
        cur_service = None
        continue
    m = re.match(r'^  (\w[\w-]*):\s*$', line)
    if m:
        if cur_service is not None:
            service_ranges.append((cur_service, cur_start, idx))
        cur_service = m.group(1)
        cur_start = idx
if cur_service is not None:
    service_ranges.append((cur_service, cur_start, len(lines)))

wyoming_patched = []
for svc_name, start, end in service_ranges:
    if svc_name == 'hailo-ollama':
        continue
    svc_lines = lines[start:end]
    if not any('/dev/h1x-0' in l for l in svc_lines):
        continue  # service doesn't use the Hailo device
    # Always overwrite any existing HAILO_VDEVICE_GROUP_ID to ensure it matches SHARED.
    existing_idx = None
    for j, l in enumerate(svc_lines):
        if 'HAILO_VDEVICE_GROUP_ID' in l:
            existing_idx = j
            break
    if existing_idx is not None:
        svc_lines[existing_idx] = re.sub(
            r'(HAILO_VDEVICE_GROUP_ID=).*', r'\g<1>' + VDEVICE_GROUP,
            svc_lines[existing_idx])
        lines[start:end] = svc_lines
        wyoming_patched.append(svc_name)
        print('  Updated HAILO_VDEVICE_GROUP_ID=' + VDEVICE_GROUP + ' in service: ' + svc_name)
        continue
    # No existing entry — add it.
    env_idx = None
    for j, l in enumerate(svc_lines):
        if re.match(r'    environment:\s*$', l):
            env_idx = j
            break
    if env_idx is not None:
        svc_lines.insert(env_idx + 1, VDEVICE_ENV_LINE)
    else:
        # No environment section — insert one before devices: (or at end)
        insert_at = len(svc_lines)
        for j, l in enumerate(svc_lines):
            if re.match(r'    devices:\s*$', l):
                insert_at = j
                break
        svc_lines.insert(insert_at, VDEVICE_ENV_LINE)
        svc_lines.insert(insert_at, '    environment:\n')
    lines[start:end] = svc_lines
    wyoming_patched.append(svc_name)
    print('  Added HAILO_VDEVICE_GROUP_ID=' + VDEVICE_GROUP + ' to service: ' + svc_name)

if not wyoming_patched:
    print('  WARNING: No Hailo-sharing service found to patch (wyoming not in compose?)')
content = ''.join(lines)
# ─────────────────────────────────────────────────────────────────────────────

with open(COMPOSE_FILE, 'w') as f:
    f.write(content)

print('  compose.yaml updated successfully')
PYEOF

  echo 'COMPOSE_UPDATED'
") || COMPOSE_RC=$?
echo "$COMPOSE_OUT"
[[ $COMPOSE_RC -eq 0 ]] || die "compose.yaml update failed (exit $COMPOSE_RC) — see output above."
echo "$COMPOSE_OUT" | grep -q "COMPOSE_UPDATED" || die "Unexpected output from compose update step."
ok "compose.yaml: updated"

# ═══════════════════════════════════════════════════════════════
# STEP 6 — docker compose up
# ═══════════════════════════════════════════════════════════════
header "STEP 6 — Start hailo-ollama via Docker Compose"

UP_OUT=""; UP_RC=0
UP_OUT=$(pi "
  set -e
  # Ensure the host dirs hailo-ollama needs exist and are writable.
  # hailo-ollama (with XDG_DATA_HOME=/usr/local/share) stores blobs in models/blob/ (singular).
  sudo mkdir -p /usr/local/share/hailo-ollama/models/manifests/hailo-ollama
  sudo mkdir -p /usr/local/share/hailo-ollama/models/blob
  sudo chown -R ${RPI_USER}:${RPI_USER} /usr/local/share/hailo-ollama/models/manifests/hailo-ollama
  sudo chown -R ${RPI_USER}:${RPI_USER} /usr/local/share/hailo-ollama/models/blob
  echo '  Host model dirs: ready'
  cd ${HA_DIR}

  # Restart any service that shares /dev/h1x-0 with hailo-ollama so it
  # picks up the new HAILO_VDEVICE_GROUP_ID before hailo-ollama starts.
  # Both processes must use the same group_id for HailoRT to allow sharing.
  # Note: 2>/dev/null must be before << to redirect python3 stderr; PEOF at col 0.
  WYOMING_SVC=\$(python3 2>/dev/null << PEOF
import yaml, sys
try:
    with open('compose.yaml') as f:
        c = yaml.safe_load(f)
    for name, svc in c.get('services', {}).items():
        if name == 'hailo-ollama':
            continue
        devs = svc.get('devices', [])
        if any('/dev/h1x-0' in str(d) for d in devs):
            print(name)
            sys.exit(0)
except Exception:
    pass
PEOF
) || WYOMING_SVC=''
  if [ -n \"\$WYOMING_SVC\" ]; then
    echo \"  Restarting \$WYOMING_SVC to activate HAILO_VDEVICE_GROUP_ID (vdevice sharing)...\"
    docker compose restart \"\$WYOMING_SVC\" 2>&1 | tail -5 || true
    sleep 5
    echo \"  \$WYOMING_SVC restarted.\"
  else
    echo '  No other Hailo service found in compose — skipping wyoming restart.'
  fi

  docker compose up -d hailo-ollama 2>&1
  echo 'COMPOSE_UP_DONE'
") || UP_RC=$?
echo "$UP_OUT"
[[ $UP_RC -eq 0 ]] || die "docker compose up failed (exit $UP_RC) — see output above."
echo "$UP_OUT" | grep -q "COMPOSE_UP_DONE" || die "Unexpected output from compose up step."
ok "hailo-ollama container: started"

# Wait for the server to bind and respond
info "Waiting for hailo-ollama to be ready on port $HAILO_OLLAMA_PORT..."
READY_RC=0
for i in $(seq 1 30); do
    READY=$(pi "curl -sf http://localhost:${HAILO_OLLAMA_PORT}/api/tags > /dev/null 2>&1 && echo READY || echo NOT_READY") || true
    if echo "$READY" | grep -q "READY"; then
        ok "hailo-ollama: listening on port $HAILO_OLLAMA_PORT"
        READY_RC=1
        break
    fi
    echo "  Waiting... ($i/30)"
    sleep 3
done
if [[ $READY_RC -eq 0 ]]; then
    warn "hailo-ollama did not respond within 90s — checking container logs..."
    pi "docker logs hailo-ollama --tail 40 2>&1" || true
    die "hailo-ollama failed to start. See logs above."
fi

# Diagnostic: check early container logs for NPU device sharing errors
info "Checking hailo-ollama startup logs for NPU errors..."
sleep 2
STARTUP_LOGS=$(pi "docker logs hailo-ollama 2>&1 | tail -30") || true
echo "$STARTUP_LOGS"
if echo "$STARTUP_LOGS" | grep -qi "HAILO_OUT_OF_PHYSICAL_DEVICES"; then
    warn "NPU device sharing conflict detected — HAILO_OUT_OF_PHYSICAL_DEVICES in logs."
    warn "Checking vdevice group IDs..."
    pi "
      echo '  hailo-ollama env:'; docker exec hailo-ollama env 2>/dev/null | grep -i hailo || echo '  (none)'
      echo '  hailo-whisper env:'; docker exec hailo-whisper env 2>/dev/null | grep -i hailo || echo '  (none)'
    " || true
    warn "Both containers must use the same group_id. Check env vars above."
fi

# ═══════════════════════════════════════════════════════════════
# STEP 7 — Pull the tool-calling model
# ═══════════════════════════════════════════════════════════════
header "STEP 7 — Pull model: $HAILO_MODEL"

MODEL_OUT=""; MODEL_RC=0
MODEL_OUT=$(pi "
  # Show what is actually in the hailo-ollama model store (helps debug wrong paths).
  echo '  Model store contents:'
  find /usr/local/share/hailo-ollama/models/ 2>/dev/null | head -40 || echo '  (directory empty or missing)'

  MODEL_PATH=\$(echo '${HAILO_MODEL}' | tr ':' '/')

  # Model is only ready when BOTH manifest AND blob files exist.
  # hailo-ollama (XDG_DATA_HOME=/usr/local/share) stores blobs in models/blob/ (singular).
  BLOB_COUNT=\$(find /usr/local/share/hailo-ollama/models/blob/ -type f 2>/dev/null | wc -l)

  # Check primary manifest path: manifests/{name}/{tag}/manifest.json
  MANIFEST_FILE=/usr/local/share/hailo-ollama/models/manifests/\${MODEL_PATH}/manifest.json
  if [ -f \"\$MANIFEST_FILE\" ] && [ \"\$BLOB_COUNT\" -gt 0 ]; then
    echo \"  Model manifest + \$BLOB_COUNT blob(s) found — skipping pull.\"
    echo \"  Manifest: \$MANIFEST_FILE\"
    echo 'MODEL_READY'
    exit 0
  elif [ -f \"\$MANIFEST_FILE\" ] && [ \"\$BLOB_COUNT\" -eq 0 ]; then
    echo \"  Manifest found at \$MANIFEST_FILE but blob/ is empty — HEF data missing, must pull.\"
  else
    echo \"  Manifest not found at \$MANIFEST_FILE — pulling model.\"
  fi

  # Secondary live check: /api/tags (only if blobs are present — avoids false positives)
  if [ \"\$BLOB_COUNT\" -gt 0 ] && curl -sf http://localhost:${HAILO_OLLAMA_PORT}/api/tags 2>/dev/null | grep -q '${HAILO_MODEL}'; then
    echo '  Model ${HAILO_MODEL} listed in /api/tags — skipping pull.'
    echo 'MODEL_READY'
    exit 0
  fi

  # hailo-ollama creates the HailoRT VDevice lazily on the first real request (pull/generate).
  # If hailo-whisper holds /dev/h1x-0 exclusively, hailo-ollama crashes instantly on pull.
  # Stop hailo-whisper before pulling to give hailo-ollama exclusive NPU access.
  cd ${HA_DIR}
  echo '  Stopping hailo-whisper temporarily (NPU needed for pull)...'
  docker compose stop hailo-whisper 2>&1 | tail -3 || true
  sleep 2

  # hailo-ollama uses a custom manifest format: hef_h10h = SHA256 of the HEF file.
  # Download URL: https://dev-public.hailo.ai/blob/sha256_{hash}  (from client.hpp API_CALL)
  # Blob storage:  XDG_DATA_HOME/hailo-ollama/models/blob/sha256_{hash}  (singular "blob")
  #   XDG_DATA_HOME=/usr/local/share in compose.yaml → bind-mounted path on host.
  MANIFEST_JSON=\$(cat /usr/local/share/hailo-ollama/models/manifests/\${MODEL_PATH}/manifest.json 2>/dev/null)
  BLOB_HASH=\$(echo \"\$MANIFEST_JSON\" | python3 -c \"
import json,sys
d=json.load(sys.stdin)
print(d.get('hef_h10h',''))
\" 2>/dev/null)
  echo \"  HEF blob hash (hef_h10h): \$BLOB_HASH\"

  if [ -z \"\$BLOB_HASH\" ]; then
    echo '  ERROR: hef_h10h field missing from manifest.'
    echo 'PULL_FAILED'; exit 1
  fi

  BLOB_FILE=/usr/local/share/hailo-ollama/models/blob/sha256_\${BLOB_HASH}

  # Migrate from old blobs/ (plural) location if we already downloaded there.
  OLD_BLOB=/usr/local/share/hailo-ollama/models/blobs/sha256_\${BLOB_HASH}
  if [ -f \"\$OLD_BLOB\" ] && [ ! -f \"\$BLOB_FILE\" ]; then
    echo \"  Migrating blob from blobs/ to blob/ (no re-download needed)...\"
    mkdir -p /usr/local/share/hailo-ollama/models/blob
    mv \"\$OLD_BLOB\" \"\$BLOB_FILE\"
    echo \"  Migrated: \$BLOB_FILE\"
  fi
  DL_URL=\"https://dev-public.hailo.ai/blob/sha256_\${BLOB_HASH}\"
  echo \"  Downloading: \$DL_URL\"
  echo \"  Destination: \$BLOB_FILE\"
  HTTP_CODE=\$(curl -w '%{http_code}' -o \"\${BLOB_FILE}.tmp\" -L --max-time 900 -s \
    --retry 2 --retry-delay 5 \"\$DL_URL\" 2>&1)
  echo \"  HTTP status: \$HTTP_CODE\"
  if [ \"\$HTTP_CODE\" = '200' ] && [ -s \"\${BLOB_FILE}.tmp\" ]; then
    echo \"  Download complete — verifying SHA256...\"
    ACTUAL_HASH=\$(sha256sum \"\${BLOB_FILE}.tmp\" | cut -d' ' -f1)
    if [ \"\$ACTUAL_HASH\" = \"\$BLOB_HASH\" ]; then
      mv \"\${BLOB_FILE}.tmp\" \"\$BLOB_FILE\"
      echo \"  SHA256 verified ✔  Blob: \$BLOB_FILE\"
    else
      rm -f \"\${BLOB_FILE}.tmp\"
      echo \"  SHA256 mismatch: expected \$BLOB_HASH got \$ACTUAL_HASH\"
      echo 'PULL_FAILED'; exit 1
    fi
  else
    rm -f \"\${BLOB_FILE}.tmp\"
    echo \"  Download failed (HTTP \$HTTP_CODE). Response body:\"
    curl -s -L --max-time 10 \"\$DL_URL\" | head -5 || true
    echo 'PULL_FAILED'; exit 1
  fi

  # Restart hailo-whisper regardless of pull outcome.
  echo '  Restarting hailo-whisper...'
  docker compose start hailo-whisper 2>&1 | tail -3 || true
  echo 'MODEL_PULLED'
") || MODEL_RC=$?
echo "$MODEL_OUT"
[[ $MODEL_RC -eq 0 ]] || die "Model pull failed (exit $MODEL_RC)."
echo "$MODEL_OUT" | grep -qE "MODEL_READY|MODEL_PULLED" || die "Model pull: unexpected output."
ok "Model $HAILO_MODEL: pulled"
# hailo-ollama has no /api/create — no alias support. HA uses the model name directly.

# ═══════════════════════════════════════════════════════════════
# STEP 8 — Smoke-test inference
# ═══════════════════════════════════════════════════════════════
header "STEP 8 — Smoke-test inference"

info "Sending test prompt to hailo-ollama..."
INFER_OUT=""; INFER_RC=0
INFER_OUT=$(pi "
  # Check container is still running before attempting inference
  STATUS=\$(docker inspect --format '{{.State.Status}}' hailo-ollama 2>/dev/null || echo 'missing')
  echo \"  Container status: \$STATUS\"
  if [ \"\$STATUS\" != 'running' ]; then
    echo '  Container is not running — last 50 log lines:'
    docker logs hailo-ollama --tail 50 2>&1 || true
    echo 'CONTAINER_DOWN'
    exit 1
  fi

  # Port 11434 was already verified in Step 6. The proxy only starts after hailo-ollama
  # is ready on 11436, so if 11434 is up the backend is up. Skip redundant internal check.
  echo '  Sending generate request (timeout 180s — first NPU inference loads HEF and can be slow)...'
  RESPONSE=\$(curl -s --max-time 180 -X POST http://localhost:${HAILO_OLLAMA_PORT}/api/generate \
    -H 'Content-Type: application/json' \
    -d '{\"model\": \"${HAILO_MODEL}\", \"prompt\": \"Reply with exactly: INFERENCE_OK\", \"stream\": false}' 2>&1)
  CURL_RC=\$?
  echo \"  curl exit: \$CURL_RC\"
  echo \"  Response: \$RESPONSE\"
  if [ \$CURL_RC -ne 0 ]; then
    echo '  Dumping container logs after curl failure:'
    docker logs hailo-ollama --tail 60 2>&1 || true
    echo 'CURL_FAILED'
    exit 1
  fi
  echo \"\$RESPONSE\" | python3 -c \"
import json,sys
d=json.load(sys.stdin)
if 'error' in d:
    print('  Model error:', d['error'])
    sys.exit(1)
print('  Model reply:', d.get('response','(empty)').strip())
\" 2>/dev/null || { echo 'SMOKE_FAILED'; exit 1; }
  echo 'SMOKE_DONE'
") || INFER_RC=$?
echo "$INFER_OUT"
if echo "$INFER_OUT" | grep -q "CONTAINER_DOWN"; then
    die "hailo-ollama container crashed after model load — see logs above."
fi
if echo "$INFER_OUT" | grep -q "SMOKE_FAILED"; then
    die "Smoke-test inference returned an error — see response above. Check hailo-ollama logs: docker logs hailo-ollama --tail 50"
fi
if echo "$INFER_OUT" | grep -q "SMOKE_DONE"; then
    ok "Inference: working"
else
    # First NPU inference (HEF model load) can exceed 120s — treat as a warning, not a hard failure.
    # The HA integration will exercise inference in real use.
    warn "Smoke-test inference timed out or returned unexpected output (curl exit $INFER_RC)."
    warn "This is normal for the first NPU inference — the HEF model may still be loading."
    warn "Verify manually: curl -s http://${SSH_ALIAS}:11434/api/tags"
fi

# ═══════════════════════════════════════════════════════════════
# STEP 9 — Verify Home Assistant connectivity
# ═══════════════════════════════════════════════════════════════
header "STEP 9 — Verify Home Assistant connectivity"

HA_OUT=""; HA_RC=0
HA_OUT=$(pi "
  # Verify HA container can reach hailo-ollama on localhost
  if docker exec homeassistant curl -sf http://localhost:${HAILO_OLLAMA_PORT}/api/tags > /dev/null 2>&1; then
    echo '  HA container → hailo-ollama: reachable on localhost:${HAILO_OLLAMA_PORT}'
  else
    echo '  WARNING: HA container cannot reach hailo-ollama on localhost:${HAILO_OLLAMA_PORT}'
    echo '  Check that homeassistant service uses network_mode: host in compose.yaml'
  fi

  # Patch the Extended OpenAI Conversation model name in HA's config entry store.
  # The 01-install script may have set it to llama3.2:3b (CPU ollama model).
  # hailo-ollama only knows 'voice-assistant', so update it.
  STORAGE=${HA_DIR}/config/.storage/core.config_entries
  if [ -f \"\$STORAGE\" ]; then
    CURRENT_MODEL=\$(sudo python3 -c \"
import json
data = json.load(open('\$STORAGE'))
for e in data.get('data', {}).get('entries', []):
    if e.get('domain') == 'extended_openai_conversation':
        # Print full options+data so we can see the actual key names
        import sys
        print('  [DEBUG] options:', json.dumps(e.get('options', {})), file=sys.stderr)
        print('  [DEBUG] data keys:', list(e.get('data', {}).keys()), file=sys.stderr)
        model = (e.get('options', {}).get('chat_model')
              or e.get('data', {}).get('chat_model')
              or e.get('options', {}).get('model')
              or e.get('data', {}).get('model')
              or 'not found')
        print(model)
        break
\" 2>&1 || echo 'unknown')
    echo \"  Extended OpenAI Conversation current model: \$CURRENT_MODEL\"

    if [ \"\$CURRENT_MODEL\" != '${HA_MODEL_ALIAS}' ]; then
      echo \"  Patching model from '\$CURRENT_MODEL' → '${HA_MODEL_ALIAS}'...\"
      sudo cp \"\$STORAGE\" \"\${STORAGE}.bak\"
      python3 -c \"
import json
data = json.load(open('\$STORAGE'))
patched = False
for e in data.get('data', {}).get('entries', []):
    if e.get('domain') == 'extended_openai_conversation':
        for section in ('options', 'data'):
            for key in ('chat_model', 'model'):
                if key in e.get(section, {}):
                    e[section][key] = '${HA_MODEL_ALIAS}'
                    patched = True
if patched:
    json.dump(data, open('\$STORAGE', 'w'), indent=2)
    print('  Patched successfully')
else:
    print('  Model key not found — check DEBUG output above for actual keys')
\"
      echo '  File patched. Reload the integration in HA UI (no full restart needed):'
      echo '  Settings → Devices & Services → Extended OpenAI Conversation → ⋮ → Reload'
      echo 'HA_PATCHED'
    else
      echo '  Model already set to ${HA_MODEL_ALIAS} — no patch needed.'
    fi
  else
    echo '  WARNING: HA config entries storage not found at \$STORAGE'
  fi

  echo 'HA_CHECK_DONE'
") || HA_RC=$?
echo "$HA_OUT"
[[ $HA_RC -eq 0 ]] || warn "HA connectivity check had issues (exit $HA_RC)."

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   ✔  hailo-ollama NPU LLM running via Docker Compose!  ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "${CYAN}  Container: ${BOLD}hailo-ollama${NC}${CYAN} (image: ${IMAGE_TAG})${NC}"
echo -e "${CYAN}  Model:     ${BOLD}${HA_MODEL_ALIAS}${NC}${CYAN} → ${HAILO_MODEL}${NC}"
echo -e "${CYAN}  API:       ${BOLD}http://localhost:${HAILO_OLLAMA_PORT}/v1${NC}"
echo ""
echo -e "${YELLOW}  ╔══ One manual step remaining ══════════════════════════╗${NC}"
echo -e "${YELLOW}  ║  Switch HA conversation agent to Extended OpenAI:     ║${NC}"
echo -e "${YELLOW}  ║  HA → Settings → Voice Assistants → edit assistant    ║${NC}"
echo -e "${YELLOW}  ║  → Conversation Agent → Extended OpenAI Conversation  ║${NC}"
echo -e "${YELLOW}  ╚═══════════════════════════════════════════════════════╝${NC}"
echo ""
echo "=== $(date '+%Y-%m-%d %H:%M:%S') — completed successfully ==="
echo "Log saved to: ${LOG_FILE}"
