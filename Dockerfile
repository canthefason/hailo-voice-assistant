# ============================================================
# Dockerfile — wyoming-hailo-whisper
# Wyoming STT server: Whisper encoder on Hailo NPU, decoder on CPU.
#
# Build (automated via build-image.sh or GitHub Actions):
#   docker build \
#     --build-arg HEF=whisper_small_en_encoder.hef \
#     --build-arg WHISPER_MODEL=small.en \
#     -t canthefason/wyoming-hailo-whisper:latest \
#     .
#
# Override model at build time:
#   --build-arg WHISPER_MODEL=tiny.en  --build-arg HEF=whisper_tiny_en_encoder.hef
#   --build-arg WHISPER_MODEL=base.en  --build-arg HEF=whisper_base_en_encoder.hef
#
# hailo_platform is NOT installed in this image.
# It is bind-mounted from the Pi host at runtime (see compose.yaml):
#   /usr/lib/python3/dist-packages/hailo_platform  — Python bindings
#   /usr/lib/libhailort.so.5.x.x                   — native library
#   /usr/lib/aarch64-linux-gnu/libusb-1.0.so.0     — USB transport
# This keeps the image free of Hailo's proprietary binaries and makes it
# compatible with any HailoRT version installed on the host.
# ============================================================


ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim-bookworm

# ── Python dependencies ──────────────────────────────────────────────────────
# torch          — CPU decoder
# openai-whisper — mel spectrogram + decoder (encoder runs on NPU)
# wyoming        — Wyoming STT protocol
# numpy          — mel preprocessing and NPU buffer I/O
RUN apt-get update && apt-get install -y --no-install-recommends \
        libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        torch \
        openai-whisper \
        wyoming \
        numpy

# ── Application code ─────────────────────────────────────────────────────────
COPY wyoming_hailo_whisper/ /app/wyoming_hailo_whisper/
WORKDIR /app

# ── Pre-download Whisper model weights ───────────────────────────────────────
# Bake the decoder weights into the image so the first transcription
# doesn't stall on a network download.
# small.en ≈ 461 MB (better accuracy), tiny.en ≈ 75 MB (faster/smaller image)
ARG WHISPER_MODEL=small.en
RUN python3 -c "import whisper; whisper.load_model('${WHISPER_MODEL}')"

# ── Hailo encoder HEF ────────────────────────────────────────────────────────
# Compiled for the Hailo-10H NPU. Must match WHISPER_MODEL architecture.
ARG HEF=whisper_small_en_encoder.hef
COPY ${HEF} /opt/whisper/encoder.hef

# ── Runtime ──────────────────────────────────────────────────────────────────
EXPOSE 10300

# Persist WHISPER_MODEL as an env var so the shell-form CMD can expand it.
ENV WHISPER_MODEL=${WHISPER_MODEL}

ENTRYPOINT ["python3", "-m", "wyoming_hailo_whisper"]
# Shell form used so $WHISPER_MODEL is expanded at container start.
CMD python3 -m wyoming_hailo_whisper \
        --hef      /opt/whisper/encoder.hef \
        --uri      tcp://0.0.0.0:10300 \
        --model    $WHISPER_MODEL \
        --language en
