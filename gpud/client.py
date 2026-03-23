"""
DaemonClient — sends JSON commands to the running gpud daemon.
"""

import json
import socket
import time

from .daemon import DAEMON_API_PORT


class DaemonClient:
    def __init__(self, host: str = "127.0.0.1", port: int = DAEMON_API_PORT, timeout: float = 30.0):
        self.host    = host
        self.port    = port
        self.timeout = timeout

    def send(self, payload: dict, timeout: float = None) -> dict:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout or self.timeout)
        try:
            s.connect((self.host, self.port))
            s.sendall((json.dumps(payload) + "\n").encode())
            data = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
            return json.loads(data.strip())
        finally:
            s.close()

    def ping(self) -> bool:
        try:
            resp = self.send({"cmd": "ping"})
            return resp.get("pong") is True
        except Exception:
            return False

    def wait_ready(self, retries: int = 30, delay: float = 0.3) -> bool:
        for _ in range(retries):
            if self.ping():
                return True
            time.sleep(delay)
        return False

    def deploy(self, name: str, config: dict) -> dict:
        return self.send({"cmd": "deploy", "name": name, "config": config}, timeout=120.0)

    def redeploy(self, name: str, config: dict) -> dict:
        return self.send({"cmd": "redeploy", "name": name, "config": config}, timeout=60.0)

    def delete(self, name: str) -> dict:
        return self.send({"cmd": "delete", "name": name})

    def list_deployments(self) -> list:
        resp = self.send({"cmd": "list"})
        return resp.get("deployments", [])

    def get_deployment(self, name: str) -> dict | None:
        resp = self.send({"cmd": "get", "name": name})
        if "error" in resp:
            return None
        return resp

    def get_events(self, limit: int = 50) -> list:
        resp = self.send({"cmd": "events", "limit": limit})
        return resp.get("events", [])

    def stop_daemon(self) -> dict:
        return self.send({"cmd": "stop"})