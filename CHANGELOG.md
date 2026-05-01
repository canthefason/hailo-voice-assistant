# Changelog

All notable changes to Hailo Voice Assistant are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

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

[Unreleased]: https://github.com/canthefason/hailo-voice-assistant/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/canthefason/hailo-voice-assistant/releases/tag/v1.0.0
