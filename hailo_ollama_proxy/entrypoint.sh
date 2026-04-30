#!/bin/bash
# Entrypoint for hailo-ollama container.
#
# hailo-ollama binary and libhailort.so are bind-mounted from the Pi host
# at runtime — they are not baked into this image. See compose.yaml.
#
# Port layout:
#   11436 — hailo-ollama native server (internal only)
#   11434 — OpenAI-compatibility proxy (what Home Assistant talks to)

set -e

mkdir -p /usr/local/share/hailo-ollama/models/manifests/hailo-ollama
mkdir -p /usr/local/share/hailo-ollama/models/blob

# Start hailo-ollama on internal port 11436
OLLAMA_HOST=0.0.0.0:11436 /usr/local/bin/hailo-ollama serve &
HAILO_PID=$!

# Wait up to 15s for hailo-ollama to be ready
for i in {1..15}; do
    curl -sf http://127.0.0.1:11436/api/tags > /dev/null 2>&1 && break
    sleep 1
done

# Start the OpenAI-compatibility proxy on port 11434 (foreground)
exec python3 /usr/local/bin/hailo-ollama-proxy.py
