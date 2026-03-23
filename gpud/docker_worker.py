"""
    DockerWorkers:
        1. Runs `docker run --gpus ...` to start the container
        2. HTTP-probes the container's /health endpoint until ready
        3. Reads real GPU metrics via NVML
        4. Drains gracefully with a configurable timeout
"""

from __future__ import annotations

import threading
import time
import pathlib
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .gpu_metrics import collector as gpu_collector, nvml_available


try: 
   import docker as docker_sdk
except ImportError:
   _docker_ok = False    

# Port Allocator

class _PortPool:
   """ Thread-safe sequential port allocator starting at base """

   def __init__(self, base: int = 9000):
      self._next = base
      self._lock = threading.Lock()

   def acquire(self) -> int:
      with self._lock:
         p = self._next
         self._next += 1
         return p    

port_pool = _PortPool(base = 9000)      

class DcokerWorker: 
   """
    A single GPU replica - 1 docker container
    
    State Machine:
        pending → starting → initializing → ready ↔ busy → draining → stopped
   """

   DRAIN_TIMEOUT = 30 

   def __init__(
        self, 
        worker_id: str,
        deployment_name: str, 
        image: str,
        gpu_indices: list[int],
        host_port: int, 
        container_port : int,
        env: dict, 
        readiness_path: str ="/health",
        readiness_initial_delay: int = 10, 
        readiness_period: int = 5, 
        readiness_failure_threshold: int = 5, 
        memory: str = "16g",
        cpu_count: Optional[str] = None,
        model_cache_dir: Optional[str] = None,
        model_cache_enabled: bool = True ,
   ):
      self.worker_id = worker_id
      self.deployment_name = deployment_name
      self.image = image
      self.gpu_indices = gpu_indices
      self.host_port = host_port
      self.container_port = container_port
      self.env = env
      self.readiness_path = readiness_path
      self.readiness_initial_delay = readiness_initial_delay
      self.readiness_period = readiness_period
      self.readiness_failure_threshold = readiness_failure_threshold
      self.memory = memory
      self.cpu_count = cpu_count

      self.state = "pending"
      self.container_id: Optional[str] = None
      self.container_name: Optional[str] = None
      self.started_at: Optional[str] = None
      self.ready_at: Optional[str] = None
      self.error: Optional[str] = None
      self.requests_served = 0

      #Real GPU metrics(when ready its populated)
      self.gpu_util_pct  = 0.0
      self.vram_used_gb  = 0.0
      self.vram_total_gb = 0.0
      self.temp_c        = 0.0
      self.power_w       = 0.0
      self.model_cache_dir     = model_cache_dir
      self.model_cache_enabled = model_cache_enabled

      self._stop_event = threading.Event()
      self._thread: Optional[threading.Thread] = None

      if not _docker_ok:
         raise RuntimeError(
            "docker SDK not installed - run -> pip install docker"
         )
      self._client = docker_sdk.from_env()
   
   def start(self):
      """ Launch container in background thread."""
      self._thread = threading.Thread(
         target = self._lifecycle, 
         name = f"worker-{self.worker_id}",
         daemon = True
      )

      self._thread.start()
  
   def drain(self):
      "Signal workers to stop accepting requests and shutdown"
      log.info(f"[{self.worker_id}] draining...")
      self.state = "draining"
      self._stop_event.set()

   def is_alive(self) -> bool:
      return self.state not in ("stopped", "error")
   
   def endpoint(self) -> str:
      return f"http:/127.0.0.1:{self.host_port}"
   
   # Lifecycle
   def _lifecycle(self):
      try: 
         self._run_container()
         if self._stop_event.is_set():
            self._stop_container()
            return
         self._wait_ready()
         if self._stop_event.is_set():
            self._stop_container()
            return
         self._serve()
      except Exception as e:
         log.error(f"[{self.worker_id}] fatal: {e}")
         self.error = str(e)
         self.state = "error"
      finally:
         self._stop_container()
         self.state ="stopped" 
         log.info(f"[{self.worker_id}] stopped")

   def _run_container(self):
      self.state = "starting"
      self.started_at = _now()
      
      #Build GPU device request 
      gpu_str = ",".join(str(i) for i in self.gpu_indices)
      device_requests = [
         docker_sdk.types.DeviceRequests(
            device_ids=[gpu_str],
            capabilities=[["gpu"]]
         )
      ]

      container_name =  f"gpud-{self.worker_id}"
      log.info(
            f"[{self.worker_id}] docker run  gpu={gpu_str}  "
            f"image={self.image}  port={self.host_port}→{self.container_port}"
      )

      #Volumes for model cache
      volumes = self._build_volumes()
      if volumes:
         for host_path in volumes:
                log.info(f"[{self.worker_id}] cache mount  {host_path} → {volumes[host_path]['bind']}")
      
      try:
         
         try:
            old = self._client_container.get(container_name)
            log.info(f"[{self.worker_id}] removing stale container {container_name}")
            old.remove(force=True)
         except docker_sdk.errors.NotFound:
            pass
         
         container = self._client.containers.run(
                image           = self.image,
                name            = container_name,
                detach          = True,
                remove          = True,           # auto-remove on stop
                device_requests = device_requests,
                ports           = {f"{self.container_port}/tcp": self.host_port},
                environment     = self.env,
                mem_limit       = self.memory,
                volumes         = volumes or None,
                # share IPC for faster GPU<->CPU transfer (vLLM needs this)
                ipc_mode        = "host",
                network_mode    = "bridge",
         )
         self.container_id = container.id
         self.container_name = container_name
         self.state = "initializing"
         log.info(f"[{self.worker_id}] container started  id={container.id[:12]}")

      except docker_sdk.errors.ImageNotFound:
         raise RuntimeError(f"image not found: {self.image}  — run: docker pull {self.image}")
      except docker_sdk.errors.APIError as e:
         raise RuntimeError(f"docker API error: {e}")   

   def _build_volumes(self) -> dict:
      """
        Return Docker Volume-mount dict for model weigth caching
        
        Mount the host cache directory into the conatiner path wher ethe model server uses
        to store weights:
            - Ollama: /root/.ollama/
            - vLLM: /root/.cache/huggingface/hub
            - generic: /model-cache ( set Model_cache_dir env in container )

      """

      if not self.model_cache_enabled or not self.model_cache_dir:
         return {}
      
      hostpath = pathlib.Path(self.model_cache_dir)
      hostpath.mkdir(parents=True, exist_ok=True)

      image = self.image.lower()
      if "ollama" in image:
         container_path = "/root/.ollama/"
      elif "vllm" in image:
         container_path = "/root/.cache/huggingface/hub"
      else:
         container_path = "/model-cache"     

      return {
         str(hostpath) : {
            "bind" : container_path,
            "mode" : "rw"
         }
      }
   
   def _wait_ready(self):
      """
        Polls containers health endpoint untill it responds 200. 
      """

      url = f"{self.endpoint()}{self.readiness_path}"
      log.info(f"[{self.worker_id}] waiting for readiness  url={url}  "
                 f"initial_delay={self.readiness_initial_delay}s")
      time.sleep(self.readiness_initial_delay)

      failures = 0 
      while not self._stop_event.is_set():
         try: 
            req = urllib.request.Request(url, method="GET")
            resp = urllib.request.urlopen(req, timeout=5)
            if resp.status < 400:
               self.state = "ready"
               self.ready_at = _now()
               log.ok(f"[{self.worker_id}] ready  endpoint={self.endpoint()}")
               return 
         except Exception as e:
            failures += 1
            log.debug(f"[{self.worker_id}] health probe fail #{failures}: {e}")
            if failures >= self.readiness_failure_threshold * 10:
               raise RuntimeError(
                        f"readiness probe failed {failures} times — "
                        f"container may have crashed. Check: docker logs {self.container_name}"
                    )
         # check if container is not dead
         self._check_container_alive()
         time.sleep(self.readiness_period)      

   def _serve(self):
      """
        Woker Ready -> poll GPU metrics until worker is drained
      """
      while not self._stop_event.is_set():
         self._refresh_gpu_metrics()
         self._check_container_alive()
         time.sleep(1.0)

      deadline = time.time() + self.DRAIN_TIMEOUT
      while self.state == "draining" and time.time() < deadline:
         time.sleep(0.5)   

   def _stop_conatiner(self):
      if not self.container_id:
         return 
      
      try: 
         c = self._client.container.get(self.container_id)
         log.info(f"[{self.worker_id}] stopping container {self.container_id[:12]} …")
         c.stop(timeout=10)
      except docker_sdk.errors.NotFound:
         pass
      except Exception as e:
         log.warn(f"[{self.worker_id}] stop error: {e}")
      finally: 
         self.container_id = None      

   def _refersh_gpu_metrics(self):
      if not nvml_available() or not self.gpu_indices:
         return 
      gpus = []
      for idx in self.gpu_indices:
         g = gpu_collector.gpu(idx)
         if g: 
            gpus.append(g)
      if not gpus: 
         return
      
      self.gpu_util_pct  = sum(g.util_pct   for g in gpus) / len(gpus)
      self.vram_used_gb  = sum(g.used_mem_gb for g in gpus)
      self.vram_total_gb = sum(g.total_mem_gb for g in gpus)
      self.temp_c        = max(g.temp_c      for g in gpus)
      self.power_w       = sum(g.power_w     for g in gpus)

      if self.state == "ready" and self.gpu_util_pct > 60:
         self.state = "busy"
      elif self.state == "busy" and self.gpu_util_pct < 30:
         self.state = "ready"   

   def _check_container_alive(self):
      if not self.container_id:
         return 
      try: 
         c = self.containers.get(self.container_id)
         status = c.status
         if status in ("exited", "dead"):
            logs = c.logs(tail=20).decode(errors="replace")
            raise RuntimeError(
                    f"container exited unexpectedly (status={status})\n"
                    f"Last logs:\n{logs}"
                )
      except docker_sdk.errors.NotFound:
         raise RuntimeError("Container disappered")
      
   def to_dict(self) -> dict:
        return {
            "worker_id":        self.worker_id,
            "deployment":       self.deployment_name,
            "image":            self.image,
            "gpu_indices":      self.gpu_indices,
            "host_port":        self.host_port,
            "container_id":     self.container_id[:12] if self.container_id else None,
            "state":            self.state,
            "started_at":       self.started_at,
            "ready_at":         self.ready_at,
            "error":            self.error,
            "requests_served":  self.requests_served,
            "endpoint":         self.endpoint(),
            "gpu_util_pct":     round(self.gpu_util_pct, 1),
            "vram_used_gb":     round(self.vram_used_gb, 1),
            "vram_total_gb":    round(self.vram_total_gb, 1),
            "temp_c":           round(self.temp_c, 1),
            "power_w":          round(self.power_w, 1),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()