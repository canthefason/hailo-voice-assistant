# CLAUDE.md — Hailo Voice Assistant project notes

Conventions and design decisions that travel with the code. Read this before
making changes to `hailo_ollama_proxy/proxy.py` or to the system prompt the
proxy injects.

## Stack

| Service | Container | Role |
|---|---|---|
| `hailo-whisper` | `hailo-whisper` | STT — Whisper on Hailo-10H NPU via Wyoming |
| `hailo-ollama` | `hailo-ollama` | LLM — `qwen2.5:1.5b` on NPU + OpenAI-compat proxy |
| Piper | `piper` | TTS via Wyoming |
| Home Assistant | `homeassistant` | Orchestrator / voice UI |

The proxy in `hailo_ollama_proxy/proxy.py` is the only thing in this repo that
sits on the request path between HA's `Extended OpenAI Conversation`
integration and the native `hailo-ollama` server.

## Why the proxy is more than a passthrough

`hailo-ollama` does not implement `/v1/*` and rejects several constructs that
HA happily sends. The proxy bridges this. There are five layers, applied in
order on each request:

1. **`fix_json_control_chars`** — HA sends literal `U+000A` inside JSON
   strings; HailoRT rejects this. Re-encodes control chars before parse.
2. **`inject_tool_prompt`** — strips `tools`/`tool_choice` (hailo-ollama
   ignores them) and injects a worked example of the `execute_services`
   shape into the system message. On tool-result follow-up turns it skips
   the JSON example and asks for a one-sentence natural-language reply
   instead. Returns a `tool_mode` ∈ `{False, True, 'followup'}` that drives
   the response-side path.
3. **`sanitize_conversation_roles`** — converts OpenAI-only constructs
   (`assistant + tool_calls + null content`, `role:"tool"`) into plain
   `assistant`/`user` messages with stringified content. hailo-ollama's
   oatpp/C++ layer null-pointer-crashes on the originals.
4. **`sanitize_for_hailo`** (marker `hailo-sanitize-v1`) — replaces
   newlines/CR/TAB inside `messages[].content`, `prompt`, `system` with
   spaces so HailoRT's prompt renderer never re-serialises a control char.
5. **`inject_defaults`** (marker `hailo-latency-v5`) — caps `max_tokens`,
   `num_predict`, `num_ctx` for short voice prompts.

Response side runs `rewrite_tool_response` (when `tool_mode is True`) or
`truncate_followup_response` (when `tool_mode == 'followup'`).

## Design decisions for tool-call accuracy on `qwen2.5:1.5b`

These were taken across PR #17 after four rounds of live voice testing.
They exist because the model produces specific recurring failure modes
that prompt rules alone do not eliminate.

### Repair before reject

The proxy normalises model output instead of trusting it. Observed failure
modes that are repaired (not rejected):

- **Service-name aliases.** Model invents `set_brightness`,
  `set_brightness_pct`, `set_brightness_level`, `dim`, `brighten`,
  `darken` instead of `turn_on`. All collapsed to `turn_on` in
  `_normalize_brightness`.
- **Arg-key aliases.** Model invents `value`, `new_value`, `new_level`,
  `brightness` (no `_pct`), `level`, `dim_level`, `percent`, `pct` instead
  of `brightness_pct`. All folded into `brightness_pct`.
- **Misplaced `brightness_pct`.** Model emits it as a sibling of
  `service_data` rather than inside it; HA silently ignores that. Promoted
  into `service_data`.
- **Brightness in 0-255 range.** Rescaled to 0-100 when value > 100.
- **Duplicate `service_data` keys.** `_merge_duplicate_service_data` runs
  before `json.loads` so the entity_id isn't dropped by Python's
  last-write-wins behaviour.
- **Friendly-name suffix on entity_id** (`light.0x...,Guest Room Light`).
  Stripped to the bare dotted id.
- **Self-cancelling `turn_on + turn_off`** of the same entity — drop the
  `turn_off`.
- **Same-entity merge.** Two list items targeting the same
  `(domain, entity_id)` are merged so a plain `turn_on` plus a
  `turn_on`-with-brightness collapse into one well-formed call.
- **Cross-entity brightness rescue.** When the model puts the user-intended
  action on entity A and a brightness on entity B (a real-but-unrelated
  entity from HA's exposed list — Guest Room Stand Light is a frequent
  victim), copy the brightness onto A and drop the second item.
- **Multi-distinct-entity drop.** When more than one distinct entity_id
  remains after the above, keep only the first item. Project scope is
  single-device commands; in observed traffic the second entity has always
  been bogus. Loosen this rule if real multi-device commands appear.

### Reject (silently, never spoken)

- **Empty / skeletal calls** (`{"list":[{}]}`, missing domain/service/
  entity_id). `content` is blanked to `""` so HA's TTS stays silent
  instead of reading raw JSON aloud.
- **Hallucinated entity_id.** `extract_known_entities` scrapes
  `domain.object_id` tokens from the request system prompt
  (HA's "Available Devices" block); `_validate_tool_arguments` rejects any
  tool call whose `entity_id` is not in that set. Falls through to
  JSON-blanking. The user gets silence rather than HA's
  "Unable to find entity" error spoken.

### Follow-up turn handling

After the model returns a tool call and HA executes it, HA echoes the
result back with `role:"tool"` and asks the model for a natural-language
summary. Two layered defences here:

1. `inject_tool_prompt` detects the `role:"tool"` message and replaces the
   JSON-only instruction block with: "EXACTLY ONE short sentence about
   ONLY the device the user asked about, do not invent details". This
   stops the model from parroting JSON back as a "summary."
2. `truncate_followup_response` post-processes the reply: blanks any
   JSON-shaped content (model occasionally re-emits `{"list":[]}` instead
   of prose) and truncates at the first sentence boundary. The first
   sentence is reliably accurate; subsequent sentences hallucinate
   irrelevant device statuses ("the bedroom lights remained off, the
   table lights were turned off, …") and invented brightness values.

### Prompt example ordering

Brightness examples FIRST, plain on/off LAST. This was tested both ways
in round-3: putting plain on/off at the top regressed dim accuracy from
6/8 to 3/8 without fixing the on/off entity hallucination it was meant to
address. Brightness-first stays.

### Known unfixable in proxy

- **"darker" semantic miss.** Model assigns `brightness_pct: 80` for
  "darker" — same value as "brighter". Tool call is well-formed; the
  number is wrong. Model bug.
- **On/off entity hallucination.** Model intermittently picks a
  Zigbee-style id (`light.0x001788010315dcf2`) for "office lights". The
  allowlist silences the failure but the action does not run. Would need
  retry-on-rejection or a different model.
- **Lying follow-up.** Model emits a clean `turn_on` *without*
  `brightness_pct`, then says "dimmed to 20 percent" anyway. Future work:
  cross-check follow-up text against the tool-call arguments.

## Live voice testing

Tests run against `rpi.local` with the deployed branch image:

```bash
curl -s -X POST -H "Authorization: Bearer $HOME_ASSISTANT" \
    -H "Content-Type: application/json" \
    -d '{"text":"<command>","agent_id":"conversation.extended_openai_conversation"}' \
    http://rpi.local:8123/api/conversation/process
```

Inspect proxy traffic with:
```bash
ssh hailo-pi 'docker logs hailo-ollama --tail 200'
```

Run `--log-level trace` (default in deployed compose) for full bodies; use
`debug` for system-prompt truncation.

A reliable end-to-end loop takes ~30s between commands (model gen ~10-15s
per turn, two turns per voice command). Tighter than that and HA may
return HTTP 500 from concurrent in-flight requests; this is a test-harness
artefact, not a real regression.

## NPU sharing

Both `hailo-whisper` and `hailo-ollama` open `/dev/h1x-0`. They must share
a `HAILO_VDEVICE_GROUP_ID=SHARED` env var or HailoRT refuses concurrent
access. Stop `hailo-whisper` before pulling/installing a new model on
`hailo-ollama` — model download triggers VDevice init and races with the
whisper container.

## Build / deploy loop

`.github/workflows/build-and-push.yml` builds `canthefason/hailo-ollama`
on every push to a feature branch. Image tag = `git rev-parse --short HEAD`.
After build:

```bash
# update the tag in the Pi's compose file and bounce the service
ssh hailo-pi "sed -i.bak \
    's|canthefason/hailo-ollama:[a-f0-9]*|canthefason/hailo-ollama:<sha>|' \
    ~/homeassistant/compose.yaml && \
    cd ~/homeassistant && \
    docker compose pull hailo-ollama && \
    docker compose up -d hailo-ollama"
```
