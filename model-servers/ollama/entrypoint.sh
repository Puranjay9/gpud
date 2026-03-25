#!/bin/bash
# Start Ollama, pull model, then start the health/proxy server

set -e

echo "[gpud-ollama] starting ollama server …"
ollama serve &
OLLAMA_PID=$!

# Wait for ollama to be ready
echo "[gpud-ollama] waiting for ollama …"
for i in $(seq 1 60); do
    if curl -sf http://localhost:11434/api/tags > /dev/null 2>&1; then
        echo "[gpud-ollama] ollama ready"
        break
    fi
    sleep 2
done

# Pull model if not already present
if ! ollama list | grep -q "${MODEL_NAME}"; then
    echo "[gpud-ollama] pulling model ${MODEL_NAME} …"
    ollama pull "${MODEL_NAME}"
else
    echo "[gpud-ollama] model ${MODEL_NAME} already cached"
fi

echo "[gpud-ollama] starting health proxy on port ${PORT} …"
python3 /health_proxy.py &
PROXY_PID=$!

# Keep both running
wait $OLLAMA_PID