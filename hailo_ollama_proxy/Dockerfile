# ============================================================
# Dockerfile — hailo-ollama
# OpenAI-compatibility proxy wrapping Hailo's hailo-ollama server.
#
# This image contains ONLY our proxy code and runtime dependencies.
# The hailo-ollama binary and HailoRT libraries are bind-mounted
# from the Pi host at runtime — they are never baked in.
# This makes the image freely publishable without redistributing
# Hailo's proprietary binaries.
#
# Required host bind-mounts (see compose.yaml):
#   /usr/local/bin/hailo-ollama        — hailo-ollama server binary
#   /usr/lib/libhailort.so.5.x.x       — HailoRT native library
#   /usr/lib/aarch64-linux-gnu/libusb-1.0.so.0 — USB transport
#   /usr/local/share/hailo-ollama      — model storage (read/write)
#
# Port layout:
#   11434 — proxy (exposed; what Home Assistant talks to)
#   11436 — hailo-ollama native server (internal only)
# ============================================================

FROM ubuntu:24.04

# Runtime deps:
#   python3      — OpenAI-compat proxy
#   curl         — hailo-ollama readiness probe in entrypoint
#   libssl3      — hailo-ollama TLS
#   libstdc++6   — C++ runtime for hailo-ollama
#   libgcc-s1    — GCC support library
#   libusb-1.0-0 — USB transport for HailoRT (fallback if not bind-mounted)
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pytest \
        curl \
        libssl3 \
        libstdc++6 \
        libgcc-s1 \
        libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Run unit tests before installing the proxy so a regression fails the build.
# Tests need proxy.py importable by its original name, so we stage in /tmp.
COPY proxy.py      /tmp/proxy.py
COPY test_proxy.py /tmp/test_proxy.py
RUN cd /tmp && python3 -m pytest test_proxy.py -q && rm proxy.py test_proxy.py

# Proxy, prompt config, and entrypoint — our code only, no Hailo IP
COPY proxy.py      /usr/local/bin/hailo-ollama-proxy.py
COPY prompts.json  /usr/local/bin/prompts.json
COPY entrypoint.sh /usr/local/bin/hailo-ollama-entrypoint.sh
RUN chmod +x /usr/local/bin/hailo-ollama-entrypoint.sh

EXPOSE 11434

ENV HAILO_INTERNAL_PORT=11436
ENV OLLAMA_PROXY_PORT=11434
ENV OLLAMA_HOST=0.0.0.0:11436
ENV OLLAMA_KEEP_ALIVE=-1

ENTRYPOINT ["/usr/local/bin/hailo-ollama-entrypoint.sh"]
