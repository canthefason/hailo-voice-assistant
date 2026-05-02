# Changelog

All notable changes to Hailo Voice Assistant are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

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

[Unreleased]: https://github.com/canthefason/hailo-voice-assistant/compare/v1.0.3...HEAD
[1.0.3]: https://github.com/canthefason/hailo-voice-assistant/compare/v1.0.2...v1.0.3
[1.0.2]: https://github.com/canthefason/hailo-voice-assistant/compare/v1.0.1...v1.0.2
[1.0.1]: https://github.com/canthefason/hailo-voice-assistant/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/canthefason/hailo-voice-assistant/releases/tag/v1.0.0
