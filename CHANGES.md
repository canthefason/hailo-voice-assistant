# Hailo Voice Assistant — Change Summary

## Proxy unit tests + CI gate (current branch)

**What changed:** Added 108 unit tests covering every pure function in
`proxy.py` (`fix_json_control_chars`, `sanitize_for_hailo`, `inject_defaults`,
`sanitize_conversation_roles`, `inject_tool_prompt`, `rewrite_tool_response`,
`truncate_followup_response`, brightness normalisation, entity coalescing,
tool-call parsing/validation, and more). Tests live in
`hailo_ollama_proxy/test_proxy.py` and use `unittest` + `pytest`.

**Build pipeline changes:**
- `Dockerfile` gains a `RUN pytest` layer (staging in `/tmp` so `import proxy`
  resolves by the original filename). A regression now fails the Docker build.
- CI workflow gains a fast `test-proxy` job that runs natively on the
  `ubuntu-latest` runner (no QEMU, ~5 s) and gates the `build-hailo-ollama`
  job — failures surface immediately without waiting for a full arm64 build.

**Also included:** `prompts.json` and the `--config` / `PROXY_CONFIG` CLI arg
that loads the three prompt strings (`example`, `instruction_template`,
`followup_hint`) at proxy startup, extracted from the inline defaults that
were previously hardcoded.

---

## Core fix: Speech2Text genai API (PR #7)

**Problem:** `hailo-whisper` was failing at startup with `HAILO_INVALID_ARGUMENT(2)` on every boot.

**Root cause:** `core.py` was using the low-level `VDevice.create_infer_model() / configure()` API. The HEF bundled with HailoRT 5.x (`whisper_small_10s_no_kqs_decoder`) is a combined encoder+decoder model — this API is incompatible with that HEF and raises the error unconditionally.

**Fix:** Rewrote `core.py` to use `hailo_platform.genai.Speech2Text`, which handles the full pipeline (mel spectrogram → encoder → decoder) entirely on the Hailo-10H NPU.

Key changes:
- `params.group_id = "SHARED"` replaces the incorrect `scheduling_algorithm` / `multi_process_service` params — this is the correct HailoRT 5.x way to share the NPU between `hailo-whisper` and `hailo-ollama` concurrently.
- `transcribe()` now converts PCM-16 bytes to little-endian float32 and calls `Speech2Text.generate_all_segments()` directly.
- Removed `torch` and `openai-whisper` from the image entirely — the NPU handles everything. Image is ~1 GB smaller and builds ~10× faster.

---

## Automated CI builds (PR #1)

Added a GitHub Actions workflow (`.github/workflows/build-and-push.yml`) that builds and pushes the `wyoming-hailo-whisper` Docker image automatically:

- Cross-compiles for `linux/arm64` on the GitHub-hosted `ubuntu-latest` runner using QEMU + `docker buildx`.
- Downloads the encoder HEF automatically from the public Hailo CDN — no manual file upload needed.
- Caches Docker layers in GitHub Actions cache so repeat builds skip the slow `torch` install step (now removed, but cache still speeds up other layers).
- Tags images with the git SHA on every push to any branch, the git tag on version tags, and `latest` on pushes to `main`.
- `WHISPER_VERSION` repo variable controls which HEF version is fetched from the CDN.

---

## hailo-ollama split into its own image (PR #2)

Moved the `hailo-ollama` service into a separate image (`canthefason/hailo-ollama`) with its own `Dockerfile` and `entrypoint.sh` under `hailo_ollama_proxy/`.

- The image contains only the OpenAI-compatibility proxy (`proxy.py`); the `hailo-ollama` binary and `libhailort` are bind-mounted from the Pi host at runtime.
- CI workflow extended with a second job (`build-hailo-ollama`) that builds and pushes this image independently.
- `compose.yaml` updated to reference the new image.

---

## Container dependency fixes (PRs #4, #5)

Several issues discovered while getting the container running on the Pi were resolved:

| Issue | Fix |
|---|---|
| `_pyhailort.cpython-313` `.so` not found — Python version mismatch | Added `ARG PYTHON_VERSION` to Dockerfile; configurable via CI repo variable |
| `libhailort.so.4.23.0` not found — wrong SDK version | Upgraded host `hailo_platform` to 5.3.0; updated all paths in `compose.yaml` |
| `libusb GLIBC_2.38` not found — Pi's libusb requires newer glibc than Bookworm | Install `libusb-1.0-0` from apt instead of bind-mounting from Pi host |
| `error: --hef argument required` at startup | Removed exec-form `ENTRYPOINT` that was conflicting with shell-form `CMD`, swallowing all flags |
| `hailo_platform` mount path wrong | Updated bind-mount source to `/usr/local/lib/python3.13/dist-packages/hailo_platform` |

---

## Reduced bind-mount count (current branch)

Eliminated two redundant bind-mounts:

**hailo-ollama** (4 → 3 mounts): Removed `libusb-1.0.so.0` — `ubuntu:24.04` ships glibc 2.39 and already has `libusb-1.0-0` installed via apt; the Pi bind-mount was never needed.

**hailo-whisper** (3 → 2 mounts): Removed `libhailort.so.5` soname symlink — it is now created inside the image at build time with `RUN ln -sf /usr/lib/libhailort.so.${HAILORT_VERSION} /usr/lib/libhailort.so.5`. The symlink resolves correctly at runtime once the versioned `.so` is bind-mounted.

> **Note:** If you upgrade HailoRT, update `HAILORT_VERSION` in the Dockerfile build args and the `.so` path in `compose.yaml` together.

---

## Final bind-mount reference

### hailo-whisper
| Mount | Purpose |
|---|---|
| `/usr/local/lib/python3.13/dist-packages/hailo_platform` | Hailo Python bindings (proprietary, can't bundle) |
| `/usr/lib/libhailort.so.5.3.0` | HailoRT native library (proprietary, can't bundle) |

### hailo-ollama
| Mount | Purpose |
|---|---|
| `/usr/local/share/hailo-ollama` | Model storage (read/write) |
| `/usr/local/bin/hailo-ollama` | hailo-ollama server binary (proprietary) |
| `/usr/lib/libhailort.so.5.3.0` | HailoRT native library (proprietary) |
