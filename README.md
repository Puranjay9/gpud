<![CDATA[<div align="center">

```
   ██████╗ ██████╗ ██╗   ██╗██████╗
  ██╔════╝ ██╔══██╗██║   ██║██╔══██╗
  ██║  ███╗██████╔╝██║   ██║██║  ██║
  ██║   ██║██╔═══╝ ██║   ██║██║  ██║
  ╚██████╔╝██║     ╚██████╔╝██████╔╝
   ╚═════╝ ╚═╝      ╚═════╝ ╚═════╝
```

**Serverless GPU Inference on Your Own Hardware**

Scale-to-zero · Auto-scaling · OpenAI-compatible API · Cold-start Buffering

![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Linux%20(NVIDIA)-orange)

</div>

---

## What is gpud?

**gpud** is a lightweight daemon that turns any Linux machine with an NVIDIA GPU into a serverless inference platform. Deploy LLM containers, and gpud handles everything else — auto-scaling replicas based on traffic, scaling to zero when idle, buffering requests during cold starts, and load-balancing across workers via Nginx.

Think of it as a single-node, self-hosted alternative to managed GPU serverless platforms like TensorFuse, Modal, or Replicate — purpose-built for local development, demos, and small-scale deployments.

### Key Features

| Feature | Description |
|---|---|
| **Scale-to-Zero** | Workers shut down automatically when idle — no GPU memory wasted |
| **Cold-Start Buffering** | Incoming requests are queued while a worker spins up, not dropped |
| **Auto-Scaling** | Scales replicas based on RPS, GPU utilization, or queue depth |
| **OpenAI-Compatible** | Exposes `/v1/chat/completions` — drop-in replacement for OpenAI SDK |
| **Model Caching** | Persists model weights to disk so cold starts load from cache, not network |
| **GPU Metrics** | Real-time VRAM, utilization, temperature, and power via NVML |
| **Live Dashboard** | `gpud watch <name>` for a curses-based live deployment monitor |

---

## System Requirements

| Requirement | Details |
|---|---|
| **OS** | Linux (Ubuntu 20.04+, Debian 11+, Fedora 36+) |
| **GPU** | NVIDIA GPU with CUDA support (tested on RTX 3060, 4060, 4090) |
| **Drivers** | NVIDIA driver 525+ with CUDA 12.0+ |
| **Runtime** | Docker with `nvidia-container-toolkit` installed |
| **Python** | 3.11 or higher |
| **Other** | `nginx`, `git`, `pip3` |

> [!NOTE]
> gpud currently supports **Linux with NVIDIA GPUs only**. macOS, Windows (WSL2), and AMD GPUs are not supported.

---

## Installation

### One-Line Install (Recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/Puranjay9/gpud/main/install.sh | bash
```

This will:
1. Clone the repo to `~/.local/share/gpud`
2. Create a Python virtual environment and install dependencies
3. Symlink the `gpud` binary to `~/.local/bin/gpud`
4. Add `~/.local/bin` to your `PATH` if needed

After installation, restart your terminal or run:
```bash
source ~/.bashrc   # or source ~/.zshrc
```

### Manual Install

```bash
git clone https://github.com/Puranjay9/gpud.git
cd gpud
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Prerequisites

Make sure these are installed before running gpud:

```bash
# NVIDIA Container Toolkit (required for GPU containers)
# See: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html

# Nginx (used as the load balancer)
sudo apt install nginx

# Docker
# See: https://docs.docker.com/engine/install/
```

---

## Quick Start

### 1. Start the Daemon

```bash
gpud daemon start
```

### 2. Build the Ollama Model Server

```bash
cd model-servers/ollama
docker build -t gpud-ollama:latest .
```

### 3. Create a Deployment Config

```bash
gpud init --name ollama-llm
```

Edit `ollama-llm.yaml`:

```yaml
image: gpud-ollama:latest
gpu_type: rtx-local
gpus: 1

min_scale: 0        # scale-to-zero when idle
max_scale: 3

port: 8000
memory: "16Gi"

env:
  MODEL_NAME: llama3.2

readiness:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 30
  periodSeconds: 5
  failureThreshold: 5

scaling:
  metric: rps
  target: 30
  scale_up_cooldown: 30
  scale_down_cooldown: 120
```

### 4. Deploy

```bash
gpud deploy --config ollama-llm.yaml --name ollama-llm
```

### 5. Watch it Come Up

```bash
gpud watch ollama-llm
```

### 6. Send Requests

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3.2",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

---

## CLI Reference

```
gpud daemon start          Start the background daemon
gpud daemon stop           Stop the daemon
gpud daemon status         Show daemon status

gpud deploy  -c <yaml>     Deploy a model service
gpud redeploy -c <yaml>    Redeploy with updated config
gpud delete  <name>        Delete a deployment
gpud list                  List all deployments
gpud status  <name>        Show deployment details
gpud watch   <name>        Live-watch dashboard
gpud logs    [name]        View event log

gpud gpus                  Show real-time GPU metrics
gpud init    [--name N]    Generate a starter deployment YAML
```

---

## Architecture

```
                    ┌─────────────────────────────────────┐
  curl/SDK ────▶    │           Nginx (:8000)              │
                    │         load balancer                │
                    └────────────┬────────────────────────┘
                                 │
                    ┌────────────▼────────────────────────┐
                    │     RequestWatcher (:9500)           │
                    │   queues requests during cold start  │
                    └────────────┬────────────────────────┘
                                 │
                    ┌────────────▼────────────────────────┐
                    │     DockerWorker (GPU Container)     │
                    │   health_proxy → Ollama/vLLM         │
                    └─────────────────────────────────────┘
                                 │
  ┌──────────────────────────────┼──────────────────────┐
  │          AutoScaler          │      GPU Metrics      │
  │  (rps / gpu_util / queue)    │       (NVML)          │
  └──────────────────────────────┴──────────────────────┘
```

**Flow:** Requests arrive at Nginx → forwarded to the RequestWatcher. If a worker is ready, the request is proxied immediately. If idle (scale-to-zero), the request is held while the watcher triggers the autoscaler to spin up a GPU container. Once the container passes its health check, all queued requests are released.

---

## Scaling Policies

| Metric | Description | Example |
|---|---|---|
| `rps` | Requests per second per replica | Scale up when RPS/replica > 30 |
| `gpu_util` | GPU utilization percentage | Scale up when GPU% > 80 |
| `queue` | Pending request queue depth | Scale up when queue > 10 |

Configure in your deployment YAML:
```yaml
scaling:
  metric: rps
  target: 30
  scale_up_cooldown: 30     # seconds between scale-up decisions
  scale_down_cooldown: 120   # seconds before scaling down
```

---

## Supported Model Servers

| Server | Image | Status |
|---|---|---|
| **Ollama** | `gpud-ollama:latest` | ✅ Fully supported |
| **vLLM** | Custom Dockerfile | 🔧 Dockerfile included, experimental |

Build the Ollama image from `model-servers/ollama/`:

```bash
docker build -t gpud-ollama:latest model-servers/ollama/
```

---

## Load Testing

Use [hey](https://github.com/rakyll/hey) to benchmark:

```bash
# Install hey
go install github.com/rakyll/hey@latest

# 120 requests, 10 concurrent, 300s timeout
hey -n 120 -c 10 -t 300 -m POST \
  -H "Content-Type: application/json" \
  -d '{"model":"llama3.2","messages":[{"role":"user","content":"Hello!"}]}' \
  http://localhost:8000/v1/chat/completions
```

---

## Limitations & MVP Scope

> [!IMPORTANT]
> gpud is an **MVP / proof-of-concept**. It demonstrates the core serverless GPU scaling pattern but is not production-ready.

### Current Limitations

| Area | Limitation |
|---|---|
| **Single Node** | Runs on one machine only — no multi-node cluster support |
| **NVIDIA Only** | No AMD ROCm or Apple Metal support |
| **Linux Only** | No macOS or Windows support (WSL2 untested) |
| **No Auth** | API endpoint is unauthenticated — no API keys or RBAC |
| **No TLS** | HTTP only, no HTTPS/TLS termination |
| **No Streaming** | Chat completions return full response, no SSE streaming support |
| **Single Model** | Each deployment serves one model; no model multiplexing |
| **Basic Scaler** | Simple threshold-based scaling, no predictive or ML-based autoscaling |
| **No Persistent Queue** | Request queue lives in memory — lost on daemon crash |

### Not Covered (Wider Scope)

These are features a production platform (like TensorFuse, Modal, Replicate) would include:

- **Multi-node orchestration** — distributing workloads across a GPU cluster
- **Model registry & versioning** — tracking model artifacts and rollbacks
- **Spot instance support** — leveraging cheap preemptible cloud GPUs
- **Batch inference** — processing large datasets offline
- **Fine-tuning pipelines** — LoRA adapter training and serving
- **Observability** — Prometheus metrics, Grafana dashboards, distributed tracing
- **Multi-tenancy** — isolated deployments per user/team with quotas
- **Billing & metering** — usage tracking and cost attribution
- **CI/CD integration** — GitHub Actions for automated model deployment
- **A/B testing** — traffic splitting between model versions
- **Custom domain & TLS** — production networking with cert management

---

## Project Structure

```
gpud/
├── gpud/                  # Core daemon package
│   ├── cli.py             # CLI entry point & argument parsing
│   ├── daemon.py          # Main daemon — orchestration loop, API server
│   ├── docker_worker.py   # GPU container lifecycle management
│   ├── watcher.py         # Per-deployment request buffer & cold-start proxy
│   ├── nginx_manager.py   # Nginx config generation & hot-reload
│   ├── scaler.py          # Auto-scaling logic (RPS, GPU util, queue)
│   ├── gpu_allocator.py   # GPU assignment & VRAM-aware packing
│   ├── gpu_metrics.py     # Real-time GPU metrics via NVML
│   ├── rps_tracker.py     # Sliding-window RPS counter
│   ├── config.py          # Deployment YAML parser & validation
│   ├── registry.py        # Persistent deployment state (~/.gpud/)
│   ├── client.py          # CLI → daemon socket client
│   ├── display.py         # Terminal UI formatting
│   └── logger.py          # Colored logging
├── model-servers/
│   └── ollama/            # Ollama container with OpenAI-compatible proxy
│       ├── Dockerfile
│       ├── entrypoint.sh
│       └── health_proxy.py
├── install.sh             # One-line curl installer
├── setup.py               # Python package config
└── README.md
```

---

## License

MIT

---

<div align="center">
  <sub>Built as an MVP to demonstrate serverless GPU scaling concepts.</sub>
</div>
]]>
