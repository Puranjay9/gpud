"""
    
    Per deployment request buffer between nginx and worker

    Architecture:
    curl → nginx (:8000) → watcher (:9500+) → worker (:9001/ollama)

    The watcher:
        1. Receives requests from nginx
        2. If worker is ready  → forward immediately (zero overhead)
        3. If worker is idle   → hold connection, fire on_scale_trigger immediately,
                           release when set_worker_ready() is called
        4. Reports RPS to the RPSTracker so the autoscaler still works 
"""

from __future__ import annotations

import asyncio
import threading
from typing import Optional, Callable

import aiohttp
from aiohttp import web

from .logger import log


QUEUE_TIMEOUT = 300

class RequestWatcher:
    """
        Async HTTP proxy with builtin request queuing for cold start buffer
        One RequestWatcher per deployment
    """

    def __init__(
            self,
            deployment_name: str,
            watcher_port: int,
            on_scale_trigger: Callable[[], None],
            rps_tracker=None
    ):
        self.name  = deployment_name
        self.watcher_port = watcher_port
        self._on_scale = on_scale_trigger
        self._rps = rps_tracker

        self._worker_endpoint: Optional[str] = None
        self._endpoint_lock   = threading.Lock()

        # asyncio.Event is created in _run_loop once the event loop exists
        self._worker_ready: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._runner: Optional[web.AppRunner] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(
            target= self._run_loop,
            daemon=True,
            name=f"watcher-{self.name}"
        )

        self._thread.start()

        for _ in range(20):
            if self._loop is not None:
                break
            threading.Event().wait(0.05)
        log.info(f"[watcher:{self.name}] started on port {self.watcher_port}")

    def stop(self):
        if self._loop and not self._loop.is_closed():
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
    
    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        self._worker_ready = asyncio.Event()
        self._loop.run_until_complete(self._serve())
    
    async def _serve(self):
        app = web.Application()
        app.router.add_route("*", "/{path_info:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.watcher_port)
        await site.start()
        while True:
            await asyncio.sleep(3600)
    
    async def _shutdown(self):
        if self._runner:
            await self._runner.cleanup()

    def set_worker_ready(self, endpoint: str):
        "Called by Deployment when worker is ready, Unblocks queued requests"
        with self._endpoint_lock:
            self._worker_endpoint = endpoint
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._worker_ready.set)
        log.info(f"[watcher:{self.name}] worker ready at {endpoint} — releasing queue")
    
    def set_worker_gone(self):
        "Called by Deployments when all workers drain. Future Requests will be queued"
        with self._endpoint_lock:
            self._worker_endpoint = None
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._worker_ready.clear)
        log.info(f"[watcher:{self.name}] worker gone — queueing mode active")

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.watcher_port}"
    

    async def _handle(self, request: web.Request) -> web.Response:
        if self._rps:
            self._rps.record_request()
        
        # Worker already ready 
        with self._endpoint_lock:
            endpoint = self._worker_endpoint
        
        if endpoint: 
            return await self._forward(request, endpoint)
        
        #queue request, trigger scale-from-zero
        log.info(
            f"[watcher:{self.name}] worker idle — queuing "
            f"{request.method} {request.path}"
        )
        threading.Thread(target=self._on_scale, daemon=True).start()

        try: 
            await asyncio.wait_for(
                self._worker_ready.wait(),
                timeout=QUEUE_TIMEOUT
            )
        except asyncio.TimeoutError:
            log.warn(f"[watcher:{self.name}] request timed out waiting for worker")
            return web.Response(
                status=503,
                text=f"worker for {self.name} did not become ready within {QUEUE_TIMEOUT}s\n",
            )
        with self._endpoint_lock:
            endpoint = self._worker_endpoint
        
        if not endpoint:
            return web.Response(status=502, text="worker endpoint lost after ready signal\n")

        log.info(f"[watcher:{self.name}] forwarding queued request")

        return await self._forward(request, endpoint)
    
    async def _forward(self, request: web.Request, endpoint: str) -> web.Response:
        path = request.match_info.get("path_info", "")
        url = endpoint.rstrip("/") + "/" + path
        if request.query_string:
            url += "?" + request.query_string
        
        body = await request.read()

        skip = {
            "host", "transfer-encoding", "connection", "keep-alive",
            "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade",
        }
        headers = {k: v for k, v in request.headers.items() if k.lower() not in skip}

        try:
            timeout = aiohttp.ClientTimeout(total=300)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method  = request.method,
                    url     = url,
                    headers = headers,
                    data    = body,
                ) as resp:
                    resp_body = await resp.read()
                    resp_headers = {
                        k: v for k, v in resp.headers.items()
                        if k.lower() not in ("transfer-encoding", "connection")
                    }

                    return web.Response(
                        status=resp.status,
                        headers = resp_headers,
                        body = resp_body,
                    )
        except aiohttp.ClientConnectionError as e:
            log.warn(f"[watcher:{self.name}] forward failed: {e}")
            self.set_worker_gone()
            return web.Response(status=502, text=f"worker connection failed: {e}\n")
        except Exception as e:
            log.error(f"[watcher:{self.name}] unexpected error: {e}")
            return web.Response(status=500, text=str(e))