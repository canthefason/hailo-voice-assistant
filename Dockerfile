# ============================================================
# Dockerfile — wyoming-hailo-whisper
# Wyoming STT server: full speech-to-text pipeline on Hailo NPU via genai API.
#
# Build (automated via GitHub Actions):
#   docker build \
#     --build-arg HEF=whisper_small_en_encoder.hef \
#     -t canthefason/wyoming-hailo-whisper:latest \
#     .
#
# The Speech2Text genai API handles mel spectrogram + encoder + decoder
# entirely on the Hailo-10H NPU — torch and openai-whisper are NOT needed.
#
# hailo_platform is NOT installed in this image.
# It is bind-mounted from the Pi host at runtime (see compose.yaml):
#   /usr/local/lib/python3.x/dist-packages/hailo_platform  — Python bindings
#   /usr/lib/libhailort.so.5.x.x                            — native library
# This keeps the image free of Hailo's proprietary binaries and makes it
# compatible with any HailoRT version installed on the host.
#
# PYTHON_VERSION must match the version hailo_platform was compiled for on
# the host Pi. Check with:
#   ls /usr/local/lib/python3.*/dist-packages/hailo_platform/pyhailort/
# The .so filename contains the version (e.g. cpython-313 → set to "3.13").
# Standard Raspberry Pi OS Bookworm ships Python 3.11.
# ============================================================

ARG PYTHON_VERSION=3.13
FROM python:${PYTHON_VERSION}-slim-bookworm

# ── System dependencies ──────────────────────────────────────────────────────
# libusb-1.0-0: required by libhailort for USB transport to the NPU.
# Installing from apt (not bind-mounting from Pi) avoids glibc version mismatch:
# the Pi's libusb is compiled against glibc 2.38; Bookworm only ships glibc 2.36.
RUN apt-get update && \
    apt-get install -y --no-install-recommends libusb-1.0-0 && \
    rm -rf /var/lib/apt/lists/*

# ── Hailo soname symlink ─────────────────────────────────────────────────────
# _pyhailort.so links against libhailort.so.5 (the soname), not the versioned
# filename. Create the symlink at build time — it resolves correctly at runtime
# once the versioned .so is bind-mounted from the Pi host. This eliminates the
# need to bind-mount libhailort.so.5 separately in compose.yaml.
ARG HAILORT_VERSION=5.3.0
RUN ln -sf /usr/lib/libhailort.so.${HAILORT_VERSION} /usr/lib/libhailort.so.5

# ── Python dependencies ──────────────────────────────────────────────────────
# wyoming  — Wyoming STT protocol
# numpy    — audio buffer conversion (PCM-16 → float32)
RUN pip install --no-cache-dir \
        wyoming \
        numpy

# ── Application code ─────────────────────────────────────────────────────────
COPY wyoming_hailo_whisper/ /app/wyoming_hailo_whisper/
WORKDIR /app

# ── Hailo encoder HEF ────────────────────────────────────────────────────────
# Combined encoder+decoder HEF compiled for the Hailo-10H NPU.
# Used by hailo_platform.genai.Speech2Text — not compatible with the old
# low-level InferModel API.
ARG HEF=whisper_small_en_encoder.hef
COPY ${HEF} /opt/whisper/encoder.hef

# ── Runtime ──────────────────────────────────────────────────────────────────
EXPOSE 10300

ARG WHISPER_MODEL=small.en
ENV WHISPER_MODEL=${WHISPER_MODEL}

CMD python3 -m wyoming_hailo_whisper \
        --hef      /opt/whisper/encoder.hef \
        --uri      tcp://0.0.0.0:10300 \
        --model    $WHISPER_MODEL \
        --language en
