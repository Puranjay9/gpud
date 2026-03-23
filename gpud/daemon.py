"""
    gpud scaling daemon
    Orchestrates actual Docker GPU containers, reads real NVML metrics,
    and manages an Nginx load balancer per deployment.
"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Optional


from .config import DeploymentConfig
from .docker_worker import DockerWorker, port_pool
from .nginx_manager import nginx
from .rps_tracker import RPSTracker
from .registry import DeploymentRegistry
from .watcher import RequestWatcher
from .gpu_allocator import allocator as gpu_allocator
from .scaler import AutoScaler
from .logger import log

DAEMON_STATE_FILE = pathlib.Path.home() / ".gpud" / "daemon.state"
DAEMON_LOCK_FILE = pathlib.Path.home() / ".gpud" / "daemon.pid"
DAEMON_API_PORT = 7821
TICK_INTERVAL = 3.0
WATCHER_PORT_START = 9500

class Deployment:
    
    def __init__(self, name: str, cfg: DeploymentConfig, watcher_port : int):
        self.name     = name
        self.cfg      = cfg
        self.workers: list[DockerWorker] = []
        self.status  = "deploying"
        self.created_at = _now()
        self._counter = 0
        self._lock = threading.Lock()
        self._pub_port = nginx.register_deployment(name, cfg.port)
        self._rps = RPSTracker(name)
        self._rps.start()

        self._watcher = RequestWatcher(
            deployment_name= name,
            watcher_port= watcher_port,
            on_scale_trigger= self._trigger_scale_now,
            rps_tracker= self._rps
        )

        self._watcher.start()

        nginx.update_workers(name, [self._watcher.endpoint])

        self._last_ready_endpoints: list[str] = []

    def _trigger_scale_now(self):
        #By passes the 3s tick
        with self._lock:
            active = sum(1 for w in self.workers if w.is_alive())
        if active == 0:
            log.info(f"[{self.name}] watcher triggered immediate scale-from-zero")
            self.spawn_worker()
    
    def spawn_worker(self) -> Optional[DockerWorker]:
        import uuid
        worker_suffix = uuid.uuid4().hex[:5]
        wid = f"{self.name}-{worker_suffix}"

        try: 
            gpu_indices = gpu_allocator.allocate(wid, self.cfg.gpus, self.cfg.vram_required_gb)
        except RuntimeError as e:
            log.warn(f"[{self.name}] cannot spawn: {e}")
            return None
        
        port = port_pool.acquire()

        env = {**self.cfg.env}
        env["CUDA_VISIBLE_DEVICES"] = ",".join(str(i) for i in gpu_indices)

        for secret in self.cfg.secrets:
            val = _load_secret(secret)
            if val:
                env[secret.upper().replace("-", "_")] = val
        
        import pathlib as _pl
        if self.cfg.model_cache_enabled:
            cache_dir = self.cfg.model_cache_dir or str(
                _pl.Path.home() / ".gpud" / "model-cache" / self.name
            )
        else:
            cache_dir = None
        
        w = DockerWorker(
            worker_id       = wid,
            deployment_name = self.name,
            image           = self.cfg.image,
            gpu_indices     = gpu_indices,
            host_port       = port,
            container_port  = self.cfg.port,
            env             = env,
            readiness_path  = self.cfg.readiness.path,
            readiness_initial_delay     = self.cfg.readiness.initial_delay,
            readiness_period            = self.cfg.readiness.period,
            readiness_failure_threshold = self.cfg.readiness.failure_threshold,
            memory          = self.cfg.memory,
            model_cache_dir     = cache_dir,
            model_cache_enabled = self.cfg.model_cache_enabled,
        )
        with self._lock:
            self.workers.append(w)
        w.start()
        log.info(f"[{self.name}] spawned {wid}  gpus={gpu_indices}  port={port}")
        return w
    
    def drain_worker(self, w: DockerWorker):
        w.drain()
        gpu_allocator.release(w.worker_id)
    
    def ready_count(self) -> int:
        return sum(1 for w in self.workers if w.state in ("ready", "busy"))
    
    def active_count(self) -> int:
        return sum(1 for w in self.workers if w.is_alive())
    
    def ready_endpoint(self) -> list[str]:
        return [w.endpoint() for w in self.workers if w.state in ("ready", "busy")]
    
    def tick(self, scaler: AutoScaler):
        with self._lock:
            dead = [w for w in self.workers if not w.is_alive()]
            for w in dead:
                gpu_allocator.release(w.worker_id)
                self.workers.remove(w)

        rps = self._rps.current_rps()
        ready = self.ready_count()
        active = self.active_count()

        if active == 0 and self.cfg.min_scale == 0 and rps > 0:
            log.info(f"[{self.name}] tick scale-from-zero: rps={rps}")
            self.status = "scaling"
            self.spawn_worker()
        else:
            desired = scaler.desired_replicas(
                rps=rps, ready=ready, active=active,
                min_scale=self.cfg.min_scale, max_scale=self.cfg.max_scale,
                name=self.name,
                metric=self.cfg.scaling.metric,
                target=self.cfg.scaling.target,
                scale_up_cooldown=self.cfg.scaling.scale_up_cooldown,
                scale_down_cooldown=self.cfg.scaling.scale_down_cooldown,
            )

            delta = desired - active
            if delta > 0:
                self.status = "scaling"
                for _ in range(delta):
                    self.spawn_worker()
            elif delta < 0:
                idle = sorted(
                    [w for w in self.workers if w.state == 'ready'],
                    key = lambda w: w.requests_served
                )
                for w in idle[:abs(delta)]:
                    self.drain_worker(w)
                if idle[:abs(delta)]:
                    self.status = "scaling"
            else:
                self.status = "idle" if active == 0 and self.cfg.min_scale == 0 else(
                    "running" if ready > 0 else "deploying"
                )
        
        current_ready = self.ready_endpoint()
        if current_ready != self._last_ready_endpoints:
            if current_ready:
                self._watcher.set_worker_ready(current_ready[0])
                log.info(f"[{self.name}] watcher activated: {current_ready[0]}")
            else:
                self._watcher.set_worker_gone()
                log.info(f"[{self.name}] watcher in queueing mode (no ready workers)")
            self._last_ready_endpoints = current_ready
        
    def shutdown(self):
        self._rps.stop()
        self._watcher.stop()
        with self._lock:
            for w in list(self.workers):
                self.drain_worker(w)

    def to_dict(self) -> dict:
        pub = nginx.public_endpoint(self.name)
        return {
            "name":       self.name,
            "status":     self.status,
            "endpoint":   pub,
            "created_at": self.created_at,
            "replicas": {
                "desired": self.cfg.max_scale,
                "ready":   self.ready_count(),
                "active":  self.active_count(),
            },
            "gpu_type":         self.cfg.gpu_type,
            "gpus_per_replica": self.cfg.gpus,
            "rps":              self._rps.current_rps(),
            "workers":          [w.to_dict() for w in self.workers],
        } 

class GPUDDaemon:
    def __init__(self):
        self.deployments: dict[str, Deployment] = {}
        self.scaler = AutoScaler()
        self.registry = DeploymentRegistry()
        self._running = threading.Event()
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._next_watcher_port = WATCHER_PORT_START
    
    def start(self):
        log.info("gpud daemon starting …")
        _ensure_home()
        _write_pidfile()
        self._running.set()

        for name, cfg in self.registry.load_all().items():
            log.info(f"restoring [{name}]")
            self._create_deployment(name, cfg)
        
        nginx.ensure_started()

        threading.Thread(target = self._orchestration_loop, daemon= True, name= "orchestrator").start()
        threading.Thread(target=self._api_server, daemon= True, name= "api-server").start()

        log.info(f"gpud daemon ready  (PID {os.getpid()}, API :{DAEMON_API_PORT})")
        self._emit("daemon_started", {})

        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        try:
            while self._running.is_set():
                time.sleep(0.5)
        except Exception:
            pass
        self._shutdown()
    
    def _handle_signal(self, sig, frame):
        log.info(f"signal {sig}, shutting down …")
        self._running.clear()
    
    def _shutdown(self):
        log.info("shutting down …")
        with self._lock:
            for dep in self.deployments.values():
                dep.shutdown()
        nginx.stop()
        _remove_pidfile()
        self._emit("daemon_stopped", {})
        self._save_state()
        log.info("bye.")
    
    def _orchestration_loop(self):
        while self._running.is_set():
            with self._lock:
                for dep in self.deployments.values():
                    try:
                        dep.tick(self.scaler)
                    except Exception as e:
                        log.error(f"tick [{dep.name}]: {e}")
                self._save_state()
            
            try:
                nginx.check_alive()
            except Exception as e:
                log.error(f"tick [{dep.name}]: {e}")
            time.sleep(TICK_INTERVAL)
    
    def delete(self, name: str) -> dict:
        with self._lock:
            if name not in self.deployments:
                return {"error": f"'{name}' not found"}
            self.deployments.pop(name).shutdown()
            self.registry.delete(name)
            self._emit("delete", {"name": name})
            return {"ok": True, "message": f"'{name}' deleted"}
    
    def list_deployments(self) -> list:
        with self._lock:
            return [d.to_dict() for d in self.deployments.values()]
    
    def get_deployment(self, name:str) -> dict | None:
        with self._lock:
            d = self.deployments.get(name)
            return d.to_dict() if d else None
    
    def _create_deployment(self, name: str, cfg: DeploymentConfig) -> Deployment:
        watcher_port = self._next_watcher_port
        self._next_watcher_port += 1
        dep = Deployment(name, cfg, watcher_port=watcher_port)
        self.deployments[name] = dep
        return dep 
    
    def _save_state(self):
        state = {
            "pid": os.getpid(),
            "updated_at": _now(),
            "deployments": {n: d.to_dict() for n, d in self.deployments.items()},
            "gpu_allocator": gpu_allocator.status()
        }

        DAEMON_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        try:
            DAEMON_STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception:
            pass

    def _emit(self, kind: str, data: dict):
        self._events.append({"ts": _now(), "kind": kind, **data})
        if len(self._events) > 500:
            self._events = self._events[-500:]
    
    def _api_server(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("127.0.0.1", DAEMON_API_PORT))
        except OSError as e:
            log.error(f"API bind failed: {e}")
            return 
        
        srv.listen(8)
        srv.settimeout(1.0)

        while self._running.is_set():
            try:
                conn, _ = srv.accept()
                threading.Thread(target=self._handle_conn, args=(conn,), daemon= True).start()
            except socket.timeout:
                continue

        srv.close()
    
    def _handle_conn(self, conn: socket.socket):
        try:
            data = b""
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
            if data:
                log.debug(f"dispatch: {data[:120]}")
                try:
                    msg = json.loads(data.strip())
                except Exception as e:
                    log.error(f"JSON parse error: {e} — raw: {data[:200]}")
                    conn.sendall((json.dumps({"error": "json parse: " + str(e)}) + "\n").encode())
                    return 
        except Exception as e:
            log.error(f"_handle_conn exception: {type(e).__name__}: {e}")
            import traceback
            log.error(traceback.format_exc())
            try:
                conn.sendall((json.dumps({"error": str(e)}) + "\n").encode())
            except Exception:
                pass
        finally:
            conn.close()
    
    def _dispatch(self, msg: dict) -> dict:
        cmd = msg.get("cmd")
        if cmd == "ping":
            return {"pong": True, "pid": os.getpid()}
        
        elif cmd == "deploy":
            name = msg["name"]
            try:
                cfg = DeploymentConfig.from_dict(msg["config"])
            except Exception as e:
                return {"error": str(e)}
            
            with self._lock:
                if name in self.deployments:
                    return {"error": f"'{name}' already exists — use gpud redeploy"}
            
            def _bg_deploy(n, c):
                try:
                    log.info(f"[{n}] background deploy starting...")
                    with self._lock:
                        dep = self._create_deployment(n,c)
                        self.registry.save(n,c)
                        self._emit("deploy", {"name": n, "gpu_type": c.gpu_type, "image": c.image})
                    for _ in range (max(c.min_scale, 1)):
                        dep.spawn_worker()
                    nginx.ensure_started()
                except Exception as e:
                    log.error(f"[{n}] deploy failed: {e}")
            
            threading.Thread(target=_bg_deploy, args=(name, cfg), daemon = True, name=f"deploy-{name}").start()
            return {"ok": True, "name": name, "message": f"deployment '{name}' queued"}
        
        elif cmd == "redeploy":
            name = msg["name"]
            try:
                cfg = DeploymentConfig.from_dict(msg['config'])
            except Exception as e:
                return {"error": str(e)}
            
            def _bg_redeploy(n, c):
                try:
                    log.info(f"[{n}] background redeploy starting...")
                    with self._lock:
                        if n in self.deployments:
                            self.deployments[n].shutdown()
                        
                        nginx.stop()
                        dep = self._create_deployment(n,c)
                        self.registry.save(n,c)
                        self._emit("redeploy", {"name", n})
                    
                    for _ in range(max(c.min_scale, 1)):
                        dep.spawn_worker()
                    nginx.stop()
                    nginx.ensure_started()
                    log.info(f"[{n}] background redeploy complete.")
                except Exception as e:
                    log.error(f"[{n}] redeploy failed: {e}")
            
            threading.Thread(target=_bg_redeploy, args=(name, cfg), daemon = True, name = f"redeploy-{name}").start()
            return {"ok": True, "name": name, "message": f"'{name}' redeploy queued"}
        elif cmd == "delete":
            return self.delete(msg["name"])
        elif cmd == "list":
            return {"deployments": self.list_deployments()}
        elif cmd == "get":
            d = self.get_deployment(msg["name"])
            return d if d else {"error": f"not found: {msg['name']}"}
        elif cmd == "events":
            return {"events": self.get_events(msg.get("limit", 50))}
        elif cmd  == "gpus":
            from .gpu_metrics import collector, nvml_available
            if not nvml_available():
                return {"error": "NVML unavailable — is nvidia-ml-py3 installed and GPU present?"}
            return {"gpus": [vars(g) for g in collector.all_gpus()]}
        elif cmd == "stop":
            self._running.clear()
            return {"ok": True, "message": "daemon stopping"}
        
        return {"error": f"unknown command: {cmd}"}

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _ensure_home():
    (pathlib.Path.home() / ".gpud").mkdir(parents=True, exist_ok=True)

def _write_pidfile():
    DAEMON_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    DAEMON_LOCK_FILE.write_text(str(os.getpid()))

def _remove_pidfile():
    try:
        DAEMON_LOCK_FILE.unlink()
    except FileNotFoundError:
        pass

def _load_secret(name: str) -> Optional[str]:
    env_key = name.upper().replace("-", "_")
    if val := os.environ.get(env_key):
        return val
    p = pathlib.Path.home() / ".gpud" / "secrets" / name
    if p.exists():
        return p.read_text().strip()
    return None

def is_daemon_running() -> bool:
    if not DAEMON_LOCK_FILE.exists():
        return False
    try:
        pid = int(DAEMON_LOCK_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError):
        _remove_pidfile()
        return False

def get_daemon_pid() -> Optional[int]:
    try:
        return int(DAEMON_LOCK_FILE.read_text().strip())
    except Exception:
        return None