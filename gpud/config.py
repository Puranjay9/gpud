"""
    DeploymentConfig - parses and validates deployment.yaml
"""

import pathlib
import yaml
from dataclasses import dataclass, field
from typing import Optional

VALID_GPU_TYPES = {
    "a10g", "a100-40gb", "a100-80gb", "h100-sxm", "h100-pcie",
    "l4", "l40s", "t4", "v100",
    # Local / consumer GPUs
    "rtx-local", "rtx-3060", "rtx-3070", "rtx-3080", "rtx-3090",
    "rtx-4060", "rtx-4070", "rtx-4080", "rtx-4090",
    "local",
}

@dataclass
class ReadinessProbe:
    path: str              = "/health"
    port: int              = 8000
    initial_delay: int     = 10 
    period:        int     = 5
    failure_threshold: int = 3

@dataclass
class ScalingPolicy: 
    metric: str              = "rps"
    target: float            = 30.0
    scale_up_cooldown: int   = 30
    scale_down_cooldown: int = 120

@dataclass
class DeploymentConfig:
    # Required
    image: str = ""
    gpu_type: str = "a10g"
    gpus: int = 1
    port: int = 8000
    
    #vram requirements
    vram_required_gb: float = 0

    # Scaling
    min_scale: int = 0 
    max_scale: int = 5

    # Enviroments & Secrets
    env: dict = field(default_factory=dict)
    secrets: list = field(default_factory=list)

    # Compute 
    cpu: str = "4"
    memory: str = "16Gi"
    timeout: int = 300

    # Networking
    concurrency: int = 4

    # Probes
    readiness: ReadinessProbe = field(default_factory=ReadinessProbe)

    # Scaling policy
    scaling: ScalingPolicy = field(default_factory=ScalingPolicy)

    # Model cache
    # Persists downloaded model weights to a host directory so every
    # subsequent cold-start loads from disk instead of re-downloading.
    # Defaults to ~/.gpud/model-cache/<name> — auto-created on first deploy.
    model_cache_dir: Optional[str] = None
    model_cache_enabled: bool = True

    # Optional MetaData
    name: Optional[str] = None
    description: Optional[str] = None
    region: str = "us-east-1"
    cloud: str = "aws"


    # Class Methods 

    @classmethod
    def from_yaml(cls, path:str | pathlib.Path) -> "DeploymentConfig":
        p = pathlib.Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(p) as f:
            raw = yaml.safe_load(f)
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, d: dict) -> "DeploymentConfig":
        cfg = cls()
        for key in ("image", "gpu_type", "gpus", "port", "min_scale", "max_scale",
                    "cpu", "memory", "timeout", "concurrency", "name", "description",
                    "region", "cloud", "model_cache_dir", "model_cache_enabled", "vram_required_gb"):
            if key in d:
                setattr(cfg, key, d[key])

        cfg.env     = d.get("env", {}) or {} 
        cfg.secrets = d.get("secrets", []) or []

        # Readiness Probe
        if "readiness" in d:
            r = d["readiness"]
            http = r.get("httpGet", {})
            cfg.readiness = ReadinessProbe(
                path              = http.get("path", "/health"),
                port              = http.get("port", cfg.port),
                initial_delay     = r.get("initialDelaySeconds", 10),
                period            = r.get("periodSeconds", 5),
                failure_threshold = r.get("failureThreshold", 3),
            )

        # Scaling policy
        if "scaling" in d:
            s = d["scaling"]
            cfg.scaling = ScalingPolicy(
                metric              = s.get("metric", "rps"),
                target              = float(s.get("target", 30.0)),
                scale_up_cooldown   = s.get("scale_up_cooldown", 30),
                scale_down_cooldown = s.get("scale_down_cooldown", 120),
            )

        # Mdoel Cache
        cfg.model_cache_dir  = d.get("model_cache_dir", None)
        cfg.model_cache_enabled = bool(d.get("model_cache_enabled", True))

        cfg.validate()
        return cfg

    def valdiate(self):
        errors = []
        if not self.image:
            errors.append("'image' is required")
        if self.gpu_type not in VALID_GPU_TYPES:
            errors.append(f"'gpu_type' must be one of: {', '.join(sorted(VALID_GPU_TYPES))}")
        if self.gpus < 1:
            errors.append("'gpus' must be >= 1")
        if self.min_scale < 0:
            errors.append("'min_scale' must be >= 0")
        if self.max_scale < self.min_scale:
            errors.append("'max_scale' must be >= min_scale")
        if self.max_scale > 50:
            errors.append("'max_scale' must be <= 50")
        if errors:
            raise ValueError("Invalid deployment config:\n  " + "\n  ".join(errors))

    def to_dict(self) -> dict:
        return {
            "image":       self.image,
            "gpu_type":    self.gpu_type,
            "gpus":        self.gpus,
            "port":        self.port,
            "min_scale":   self.min_scale,
            "max_scale":   self.max_scale,
            "env":         self.env,
            "secrets":     self.secrets,
            "cpu":         self.cpu,
            "memory":      self.memory,
            "timeout":     self.timeout,
            "concurrency": self.concurrency,
            "region":      self.region,
            "cloud":       self.cloud,
            "vram_required_gb": self.vram_required_gb,
            "readiness": {
                "httpGet": {"path": self.readiness.path, "port": self.readiness.port},
                "initialDelaySeconds": self.readiness.initial_delay,
                "periodSeconds":       self.readiness.period,
                "failureThreshold":    self.readiness.failure_threshold,
            },
            "scaling": {
                "metric":              self.scaling.metric,
                "target":              self.scaling.target,
                "scale_up_cooldown":   self.scaling.scale_up_cooldown,
                "scale_down_cooldown": self.scaling.scale_down_cooldown,
            },
            "model_cache_dir":     self.model_cache_dir,
            "model_cache_enabled": self.model_cache_enabled,
        }

EXAMPLE_YAML = """\
# gpud deployment configuration
# Deploy with: gpud deploy --config deployment.yaml

# --- Docker image (your model server) ---
image: my-registry/llama3-vllm:latest

# --- GPU settings ---
gpu_type: a10g       # a10g | a100-40gb | a100-80gb | h100-sxm | l4 | l40s
gpus: 1              # GPUs per replica

vram_required_gb: 0  # 0 = strict 1:1 (no sharing). Set >0 for VRAM-aware packing

# --- Serverless scaling ---
min_scale: 0         # 0 = scale-to-zero when idle
max_scale: 8         # maximum replicas

# --- Networking ---
port: 8000
concurrency: 4       # concurrent requests per replica
timeout: 300         # request timeout in seconds

# --- Resources ---
cpu: "4"
memory: "16Gi"

# --- Cloud ---
cloud: aws
region: us-east-1

# --- Environment variables ---
env:
  MODEL_NAME: meta-llama/Llama-3.1-8B-Instruct
  DTYPE: float16

# --- Secrets (stored in gpud secret store) ---
secrets:
  - huggingface-token

# --- Readiness probe ---
readiness:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 30
  periodSeconds: 10
  failureThreshold: 5

# --- Auto-scaling policy ---
scaling:
  metric: rps          # rps | gpu_util | queue_depth
  target: 30           # scale up when RPS/replica exceeds this
  scale_up_cooldown: 30
  scale_down_cooldown: 180
"""    