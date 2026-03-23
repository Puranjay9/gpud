"""
                    NGINX MANAGER
    Writes and hot-reloads an Nginx config that
    load-balances across all ready worker replicas.

    Requires: nginx installed on the host (apt install nginx).
    Config lives at /tmp/gpud-nginx/ to avoid needing root.
    Uses `nginx -p /tmp/gpud-nginx` (prefix mode) so no /etc/nginx write needed.
"""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import threading
import time
from typing import Optional

from .logger import log

NGINX_PREFIX = pathlib.Path("/tmp/gpud-nginx")
NGINX_CONF = NGINX_PREFIX / "nginx.conf"
NGINX_PIDFILE = NGINX_PREFIX / "nginx.pid"
NGINX_LOGS = NGINX_PREFIX / "logs"

def _nginx_available() -> bool:
    try: 
        r = subprocess.run(["nginx", "-v"], capture_output=True, timeout=3)
        return r.returncode == 0 or b"nginx" in r.stderr
    except FileNotFoundError:
        return False

NGINX_OK = _nginx_available()

class NginxManager:

    def __init__(self):
        self._lock  = threading.Lock()
        self._start_lock = threading.Lock() #to prevent concurrent start() calls
        self._upstreams: dict[str, list[tuple[str, int]]] = {}
        self._ports: dict[str, int] ={}

        if NGINX_OK: 
            NGINX_PREFIX.mkdir(parents=True, exist_ok=True)
            NGINX_LOGS.mkdir(parents=True, exist_ok=True)

            for d in ("client-body", "proxy", "fastcgi", "uwsgi", "scgi"):
                (NGINX_PREFIX / d).mkdir(parents=True, exist_ok=True)
                self._stop_stale()
                log.info("Nginx load balancer available")
        else:
            log.warn("nginx not found — per-worker direct endpoints will be used instead")

    def register_deployment(self, name: str, port: int) -> int:
        with self._lock:
            if name in self._ports and self._ports[name] != port:
                log.info(f"[nginx] {name} public port changing: {self._ports[name]} → {port}")
            self._ports[name] = port
            if name not in self._upstreams:
                self._upstreams[name] = []
            return port

    def update_workers(self, name: str, worker_endpoints: list[str]):

        if not NGINX_OK:
            return 

        servers = []
        for ep in worker_endpoints:
            host_port = ep.replace("http://", "").replace("https://", "")
            host, port = host_port.rsplit(":", 1)
            servers.append((host, int(port)))

        with self._lock:
            old = self._upstreams.get(name, [])

            if not servers:
                log.warn(
                    f"[nginx] {name}: ignoring empty worker list "
                    f"(keeping existing pool: {old})"
                )
                return 
            
            if servers != old:
                log.info(f"[nginx] {name} upstreams: {old} → {servers}")

            self._upstreams[name] = servers
            self._write_config()

        self._reload()

    def ensure_started(self):
        """
            Write config and start nginx if not already running.
            Called once after all deployments are registered on daemon start 
            and after each new deploy/redeploy
        """            

        if not NGINX_OK:
            return 
        with self._lock:
            self._write_config()
        self._reload()    

    def check_alive(self):
        if not NGINX_OK: 
            return 
        if not self._ports:
            return 
        if not self._is_running():
            log.warn("[nginx] process died — restarting")
            with self._lock:
                self._write_config()
            self._start()

    def public_endpoint(self, name: str) -> Optional[str]:
        port = self._ports.get(name)
        if port: 
            return f"http://localhost:{port}"
        return None

    def stop(self):
        if not NGINX_OK:
            return 

        try:
            subprocess.run(
                ["nginx", "-p", str(NGINX_PREFIX), "-s", "stop"],
                capture_output=True, timeout=5
            )           
            log.info("nginx stopped")
        except Exception as e:
            log.warn(f"nginx stop error: {e}")


    def _stop_stale(self):
        "stops any nginx left over fro last daemon run"
        if NGINX_PIDFILE.exists():
            try:
                pid = int(NGINX_PIDFILE.read_text().strip())
                os.kill(pid, signal.SIGTERM)
                for _ in range(15):
                    try: 
                        os.kill(pid, 0)
                        time.sleep(0.2)
                    except ProcessLookupError:
                        break
                log.info("stopped stale nginx")
            except Exception:
                pass

        try:
            subprocess.run(
                ["pkill", "-9", "-f", f"nginx.*{NGINX_PREFIX}"],
                capture_output=True, timeout=5
            )
            time.sleep(0.3) 
        except Exception:
            pass
        
        try:
            NGINX_PIDFILE.unlink(missing_ok=True)
        except Exception:
            pass

    def _write_config(self):
        upstream_blocks = []
        server_blocks = []

        for name, servers in self._upstreams.items():
            pub_port = self._ports.get(name, 8080)

            if not servers:
                log.debug(f"[nginx] skipping {name} — no upstreams yet")
                continue

            up_name = f"gpud_{name.replace('-', '_')}"
            srv_lines = "\n        ".join(f"server {h}:{p};" for h, p in servers)

            upstream_blocks.append(f"""
                    upstream {up_name} {{
                        least_conn;
                        keepalive 32;
                        {srv_lines}
                    }}
                """)
            
            server_blocks.append(f"""
                server {{
                    listen {pub_port};
                    access_log {NGINX_LOGS}/access.log combined;
                    error_log  {NGINX_LOGS}/error.log warn;

                    location /{{
                        proxy_pass http://{up_name};
                        proxy_http_version 1.1;
                        proxy_set_header   Connection "";
                        proxy_set_header   Host $host;
                        proxy_set_header   X-Real-IP $remote_addr;
                        proxy_read_timeout 300s;
                        proxy_send_timeout 300s;
                    }}
                }}
            """)

            if not server_blocks:
                log.debug("[nginx] no ready deployments — skipping config write")
                return 
            
            config = f"""
                worker_process auto;
                error_log {NGINX_LOGS}/error.log warn;
                pid {NGINX_PIDFILE};

                events{{
                    worker_connections 1024;
                    use epoll;
                }}

                http {{
                    client_body_temp_path {NGINX_PREFIX}/client-body;
                    proxy_temp_path       {NGINX_PREFIX}/proxy;
                    fastcgi_temp_path     {NGINX_PREFIX}/fastcgi;
                    uwsgi_temp_path       {NGINX_PREFIX}/uwsgi;
                    scgi_temp_path        {NGINX_PREFIX}/scgi;

                    {" ".join(upstream_blocks)}
                    {" ".join(server_blocks)}
                }}
            """

            NGINX_CONF.write_text(config)
    
    def _is_running(self) -> bool:
        "Check if nginx is running via pidfile, with pgrep fallback"
        if NGINX_PIDFILE.exists():
            try:
                pid = int(NGINX_PIDFILE.read_text().strip())
                os.kill(pid, 0)
                return True
            except (ProcessLookupError, ValueError, OSError):
                NGINX_PIDFILE.unlink(missing_ok=True)

        try:
            result = subprocess.run(
                ["pgrep", "-f", f"nginx.*{NGINX_PREFIX}"],
                capture_output=True, timeout=3
            )        

            if result.returncode == 0 :
                pid = int(result.stdout.strip().splitlines()[0])
                NGINX_PIDFILE.write_text(str(pid))
                log.warn(f"nginx pidfile was stale — repaired with pid {pid}")
                return True
        except Exception:
            pass 
        
        return False
    
    def _reload(self):
        "Hot reload nginx config, or start if it not running."

        if self._is_running():
            try:
                pid = int(NGINX_PIDFILE.read_text().strip())
                os.kill(pid, signal.SIGHUP)
                time.sleep(0.1)
                log.debug("nginx config reloaded")
                return 
            except Exception as e:
                log.warn(f"nginx SIGHUP failed: {e}, attempting restart")
                NGINX_PIDFILE.unlink(missing_ok=True)

        if not NGINX_CONF.exists():
            with self._lock:
                self._write_config()

        if not NGINX_CONF.exists():
            log.warn("[nginx] _reload: still no config after rewrite attempt — skipping start")
            return 

        self._start()  

    def _start(self):

        if not NGINX_CONF.exists():
            log.warn("nginx config not written yet — skipping start")
            return

        with self._start_lock:
            if self._is_running():
                log.debug("nginx already running — skipping duplicate start")
                return 

            result = subprocess.run(
                ["nginx", "-p", str(NGINX_PREFIX), "-c", str(NGINX_CONF)],
                capture_output=True, timeout=5
            )                  

            if result.returncode != 0:
                log.error(f"nginx start failed: {result.stderr.decode()}")
                return
            
            time.sleep(0.5)

            if self._is_running():
                log.ok(f"nginx load balancer started (prefix={NGINX_PREFIX})")
            else:
                try:
                    last_err = NGINX_LOGS.joinpath("error.log").read_text().strip().splitlines()[-1]
                except Exception as e:
                    last_err = "Unknown Exception: {e}"
                log.error(f"nginx started but immediately exited. Last error: {last_err}")

nginx = NginxManager()    