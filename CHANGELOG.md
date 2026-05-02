# Changelog

All notable changes to Hailo Voice Assistant are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

---

## [1.0.7] — 2026-05-01

### Fixed
- `hailo_ollama_proxy`: broken service calls no longer reach Home Assistant when the model generates structurally valid but empty tool arguments (e.g. `{"list": [{}]}`); proxy now validates that each `execute_services` list item has `domain`, `service`, and `service_data.entity_id` before rewriting to `tool_calls` format — invalid calls are logged and returned as plain text instead

### Changed
- Injected tool call example expanded from one (turn on) to two (turn on + dim to 30%) to give the model a better template for brightness commands

---

## [1.0.6] — 2026-05-01

### Fixed
- `hailo_ollama_proxy`: null-pointer crash (HTTP 500) when Home Assistant sends back conversation history after a tool call
  - After the proxy rewrites a response into `tool_calls` format, HA echoes the history back with `{"role": "assistant", "content": null, "tool_calls": [...]}` and `{"role": "tool", ...}` messages; hailo-ollama (oatpp/C++) dereferences `content` as `std::string` and crashes on null
  - Added `sanitize_conversation_roles()` to the request pipeline: converts `assistant+tool_calls+null content` to a plain assistant message with the tool call serialised as JSON text, and `role:tool` to a `role:user` message with the result

---

## [1.0.5] — 2026-05-01

### Fixed
- `hailo_ollama_proxy`: tool call JSON truncated at token limit, producing unparseable output (e.g. missing final `}`)
  - `inject_tool_prompt` now raises `max_tokens` to at least 250 when tools are present — a pretty-printed single-service call is ~150-200 tokens and the previous 119-120 cap was cutting output mid-JSON
  - `inject_defaults` no longer clamps `max_tokens` values ≤ 500; only pathologically high values (HA default: 1022) are clamped, preserving the 250 set by `inject_tool_prompt`
  - `_fix_json` now balances unmatched braces/brackets as a safety net, handling any future truncation that slips through

---

## [1.0.4] — 2026-05-01

### Fixed
- `hailo_ollama_proxy`: model JSON output with trailing commas (e.g. `[{...},]`) now parsed correctly — common LLM artifact that caused `rewrite_tool_response` to silently fall back to plain text, preventing Home Assistant from receiving the tool call
- Added `_fix_json()` helper that strips trailing commas before `]`/`}` and markdown fences; applied to all parse paths in `_try_parse_tool_call`

---

## [1.0.3] — 2026-05-01

### Added
- `hailo_ollama_proxy`: tool call emulation layer (hailo-tools-v1)
  - **Request side** (`inject_tool_prompt`): detects `tools[]` in the request, injects a strict single-line JSON output instruction and a concrete `execute_services` example into the system message, then strips `tools`/`tool_choice` before forwarding — hailo-ollama does not implement the OpenAI tool calling spec and would ignore or reject these fields
  - **Response side** (`rewrite_tool_response`): parses the model's plain-text output and rewrites it into the OpenAI `tool_calls` format Home Assistant expects (`finish_reason: "tool_calls"`, `content: null`); handles `{name, arguments}` objects, bare `{list:[]}` arguments, `func({})` call notation, and markdown-fenced JSON; falls back to the original plain-text response if no tool call is detected

---

## [1.0.2] — 2026-05-01

### Added
- `hailo_ollama_proxy`: single `--log-level info|debug|trace` arg (env: `PROXY_LOG_LEVEL`) replaces separate `--debug` / `--trace` flags
  - `info` — startup banner only (default)
  - `debug` — log every request and response body; system prompt truncated to 200 chars
  - `trace` — like debug but full bodies with no truncation; use when inspecting the full HA system prompt or entity list
- Truncation hint in debug mode shows how many chars were cut and suggests `--log-level trace`

---

## [1.0.1] — 2026-05-01

### Added
- `hailo_ollama_proxy`: all inference thresholds now configurable via CLI args — `--max-tokens`, `--num-predict`, `--num-ctx`, `--temperature`, `--top-p`, `--listen-port`, `--backend-port`
- Proxy startup log now prints the active config for easy verification via `docker logs hailo-ollama`

### Fixed
- `/v1/chat/completions` path (used by Home Assistant) was not receiving `num_ctx` injection — context window was unconstrained on every HA voice request
- Raised `num_ctx` default from 512 to 1024 to accommodate the HA system prompt + entity list without truncation
- `num_predict` cap now applies as a hard ceiling on native API paths (previously only injected when missing, not when caller sent a higher value)

---

## [1.0.0] — 2026-05-01

### Added
- Full voice assistant stack: HailoRT 5.3.0 + Wyoming Whisper (NPU encoder) + Ollama (CPU LLM) + Piper TTS + Home Assistant Wyoming integration
- `01-install-hailo-voice-assistant.sh` — automated Pi deployment script covering HailoRT, Whisper STT, Ollama, Piper, and Extended OpenAI Conversation
- `02-install-hailo-ollama-npu.sh` — Hailo-accelerated Ollama (hailo-gateway) install script
- `build-image.sh` — Docker image build pipeline for `canthefason/wyoming-hailo-whisper`; uploads source to Pi, runs `docker build`, optionally pushes to Docker Hub; auto-tags with git SHA
- `esp32.yaml` — ESPHome voice satellite configuration for ESP32-S3-BOX
- `00-setup-ssh.sh` — SSH key setup helper for Mac → Pi connectivity
- `run-install.sh` — wrapper that tees install output to `install.log` for Claude diagnosis
- `wyoming_hailo_whisper/` — Wyoming protocol server wrapping the Hailo NPU Whisper encoder
- Release management pipeline: `VERSION`, `CHANGELOG.md`, `bump-version.sh`, `release.sh`

### Changed
- Replaced `InferModel` API with `hailo_platform.genai.Speech2Text` for HailoRT 5.x compatibility (#7)
- Switched to `ROUND_ROBIN` scheduler for HailoRT 5.x `InferModel` API (#6)
- Reduced bind-mount count for `hailo-ollama` and `hailo-whisper` containers (#8)
- Separated Hailo Ollama NPU install into its own script (#2)
- Switched default Whisper model to `whisper-small`; added automated HEF download from Hailo public CDN

### Fixed
- `hailo-whisper` container dependency and Python version issues (#4)
- Entrypoint script fixes (#5)

---

[Unreleased]: https://github.com/canthefason/hailo-voice-assistant/compare/v1.0.7...HEAD
[1.0.7]: https://github.com/canthefason/hailo-voice-assistant/compare/v1.0.6...v1.0.7
[1.0.6]: https://github.com/canthefason/hailo-voice-assistant/compare/v1.0.5...v1.0.6
[1.0.5]: https://github.com/canthefason/hailo-voice-assistant/compare/v1.0.4...v1.0.5
[1.0.4]: https://github.com/canthefason/hailo-voice-assistant/compare/v1.0.3...v1.0.4
[1.0.3]: https://github.com/canthefason/hailo-voice-assistant/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/canthefason/hailo-voice-assistant/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/canthefason/hailo-voice-assistant/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/canthefason/hailo-voice-assistant/releases/tag/v1.0.0
