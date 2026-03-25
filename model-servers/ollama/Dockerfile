# model-servers/ollama/Dockerfile
# Ollama GPU server with an OpenAI-compatible /health endpoint.
# Smaller VRAM footprint than vLLM — good for RTX 3060/4060 (8-12GB).
#
# Build:
#   docker build -t gpud-ollama:latest .
#
# Run standalone:
#   docker run --gpus all -p 8000:8000 \
#     -e MODEL_NAME=llama3.2 \
#     gpud-ollama:latest

FROM ollama/ollama:latest

RUN apt-get update && apt-get install -y \
    curl python3 python3-pip \
    --no-install-recommends \
 && pip3 install --no-cache-dir flask requests --break-system-packages \
 && rm -rf /var/lib/apt/lists/*

ENV MODEL_NAME="llama3.2"
ENV PORT="8000"
ENV OLLAMA_HOST="0.0.0.0:11434"

EXPOSE 8000

COPY entrypoint.sh /entrypoint.sh
COPY health_proxy.py /health_proxy.py
RUN chmod +x /entrypoint.sh

HEALTHCHECK --interval=15s --timeout=10s --start-period=120s --retries=8 \
    CMD curl -sf http://localhost:${PORT}/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
