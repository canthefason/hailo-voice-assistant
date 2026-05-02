#!/usr/bin/env bash
# ============================================================
# voice-test.sh — Send voice commands to Home Assistant's
# Extended OpenAI Conversation agent and report each spoken reply.
#
# Designed for live-testing hailo-ollama proxy changes against
# whatever image is currently deployed on the Pi.
#
# Usage:
#   bash voice-test.sh                      # default 8-command suite
#   bash voice-test.sh "turn on the lights" # one ad-hoc command
#   bash voice-test.sh -f commands.txt      # one command per line
#   bash voice-test.sh --log                # also dump proxy traces from this run
#   bash voice-test.sh --interval 30        # seconds between commands (default 45)
#
# Env:
#   HOME_ASSISTANT  Bearer token (required)
#   HA_URL          default http://rpi.local:8123
#   HA_AGENT_ID     default conversation.extended_openai_conversation
#   PI_SSH          default hailo-pi (only used with --log)
#
# Result legend:
#   ✔  speech reply received (action probably ran)
#   ⊘  empty speech (proxy blanked a JSON-shaped or rejected reply;
#                    action may or may not have run — check --log)
#   ✘  HA error speech ("Something went wrong: …") or HTTP/parse failure
# ============================================================
set -euo pipefail

HA_URL="${HA_URL:-http://rpi.local:8123}"
HA_AGENT_ID="${HA_AGENT_ID:-conversation.extended_openai_conversation}"
PI_SSH="${PI_SSH:-hailo-pi}"

INTERVAL=45
FETCH_LOGS=0
COMMANDS_FILE=""
ARGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        -f|--file)         COMMANDS_FILE="$2"; shift 2 ;;
        --interval)        INTERVAL="$2"; shift 2 ;;
        --log|--logs)      FETCH_LOGS=1; shift ;;
        -h|--help)
            sed -n '/^# voice-test.sh/,/^# ====/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)                 ARGS+=("$1"); shift ;;
    esac
done

[[ -n "${HOME_ASSISTANT:-}" ]] || {
    echo "✘ HOME_ASSISTANT env var is required (HA bearer token)" >&2
    exit 1
}

if [[ -n "$COMMANDS_FILE" ]]; then
    mapfile -t commands < <(grep -v '^[[:space:]]*#' "$COMMANDS_FILE" | grep -v '^[[:space:]]*$')
elif [[ ${#ARGS[@]} -gt 0 ]]; then
    commands=("${ARGS[@]}")
else
    commands=(
        "dim the office lights to 20 percent"
        "set the office lights to 50 percent"
        "dim the office lights to thirty"
        "office lights at 70 percent"
        "make the office lights brighter"
        "make the office lights darker"
        "turn on the office lights"
        "turn off the office lights"
    )
fi

# Snapshot proxy log offset so --log only shows lines from this run.
log_offset=0
if [[ "$FETCH_LOGS" -eq 1 ]]; then
    log_offset=$(ssh "$PI_SSH" 'docker logs hailo-ollama 2>&1 | wc -l')
fi

pass=0
silent=0
fail=0
total=${#commands[@]}
i=0

for cmd in "${commands[@]}"; do
    i=$((i+1))
    echo "════════════════════════════════════════════════════════════"
    printf '[%d/%d] %s\n' "$i" "$total" "$cmd"

    payload=$(python3 -c '
import json, sys
print(json.dumps({"text": sys.argv[1], "agent_id": sys.argv[2]}))
' "$cmd" "$HA_AGENT_ID")

    t0=$(date +%s)
    resp=$(curl -s --max-time 90 \
        -H "Authorization: Bearer $HOME_ASSISTANT" \
        -H "Content-Type: application/json" \
        -d "$payload" \
        "$HA_URL/api/conversation/process") || resp=""
    t1=$(date +%s)
    duration=$((t1 - t0))

    # Extract the spoken reply; tolerate non-JSON HA error pages.
    speech=$(printf '%s' "$resp" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get("response", {}).get("speech", {}).get("plain", {}).get("speech", ""))
except Exception:
    print("__HTTP_PARSE_ERROR__")
' 2>/dev/null)

    if [[ "$speech" == "__HTTP_PARSE_ERROR__" ]]; then
        printf '  ✘  %2ds  HTTP/JSON error: %s\n' "$duration" "${resp:0:80}"
        fail=$((fail + 1))
    elif [[ -z "$speech" ]]; then
        printf '  ⊘  %2ds  (silent)\n' "$duration"
        silent=$((silent + 1))
    elif [[ "$speech" == "Something went wrong"* ]]; then
        printf '  ✘  %2ds  %s\n' "$duration" "$speech"
        fail=$((fail + 1))
    else
        printf '  ✔  %2ds  %s\n' "$duration" "$speech"
        pass=$((pass + 1))
    fi

    # Skip the trailing sleep on the last command.
    if [[ "$i" -lt "$total" ]]; then
        sleep "$INTERVAL"
    fi
done

echo "════════════════════════════════════════════════════════════"
printf 'Summary: %d ✔   %d ⊘   %d ✘   /  %d total\n' "$pass" "$silent" "$fail" "$total"

if [[ "$FETCH_LOGS" -eq 1 ]]; then
    echo "════════════════════════════════════════════════════════════"
    echo "Proxy log markers from this run:"
    ssh "$PI_SSH" "docker logs hailo-ollama 2>&1 | tail -n +$log_offset" \
        | grep -E '^\[proxy\] (tool call|suppressed|entity_id|retrying)' \
        || echo "  (no proxy markers — all commands took the happy path)"
fi
