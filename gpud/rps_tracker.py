"""
            RPS Tracker
        Uses Nginx logs tailing for real rps measurements

        Fallsback to http polling if log not available 
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from datetime import datetime

# Nginx combined log format timestamp: [10/Oct/2000:13:55:36 -0700]
_TS_RE = re.compile(r'\[(\d{2}/\w+/\d{4}:\d{2}:\d{2}:\d{2})')

NGINX_ACCESS_LOG = "/tmp/gpud-nginx/logs/access.log"
WINDOW_SECONDS   = 10

class RPSTracker:

    def __init__(self, deployment_name: str):
        self.name = deployment_name
        self._lock = threading.Lock()
        self._timestamps: deque[float] = deque()
        self._log_pos = 0 
        self._running = threading.Event()
        self._thread  = threading.Thread | None = None


    def start(self):
        self._running.set()
        self._thread = threading.Thread(
            target=self._tail_nginx,
            daemon=True,
            name=f"rps-{self.name}"
        )
        self._thread.start

    def stop(self):
        self._running.clear()
    
    def record_requests(self):
        with self._lock:
            self._timestamps.append(time.time())

    def current_rps(self) -> float:
        now = time.time()
        cutoff = now - WINDOW_SECONDS

        with self._lock:
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            count = len(self._timestamps)
         
        return round(count / WINDOW_SECONDS, 2)
    
    def _tail_nginx(self):
        
        while self._running.is_set() and os.path.exists(NGINX_ACCESS_LOG):
            time.sleep(1.0)
        if not self._running.is_set():
            return 
        
        try:
            with open(NGINX_ACCESS_LOG, "r") as f:
                f.seek(0, 2)
                while self._running.is_set():
                    line = f.readline()
                    if not line:
                        time.sleep(0.05)
                        continue
                    
                    ts = _parse_nginx_ts(line)
                    with self._lock:
                        self._timestamps.append(ts or time.time())
        except FileNotFoundError:
            pass # nginx not running fallback to record_request()
        except Exception as e:
            pass

def _parse_nginx_ts(line: str) -> float | None:
    m = _TS_RE.search(line)
    if not m:
        return None
    try: 
        dt = datetime.strptime(m.group(1), "%d/%b/%Y:%H:%M:%S")
        return dt.timestamp()
    except Exception:
        return None