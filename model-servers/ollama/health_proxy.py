"""
health_proxy.py — Runs inside the Ollama container.
Exposes:
  GET  /health        → 200 OK (for gpud readiness probe)
  POST /v1/chat/completions  → proxied to Ollama's native API
  GET  /v1/models     → list loaded models
"""

import json
import os
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler

OLLAMA_BASE = "http://localhost:11434"
PORT        = int(os.environ.get("PORT", 8000))
MODEL_NAME  = os.environ.get("MODEL_NAME", "llama3.2")


def ollama_ready() -> bool:
    try:
        urllib.request.urlopen(f"{OLLAMA_BASE}/api/tags", timeout=2)
        return True
    except Exception:
        return False


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress access log spam

    def do_GET(self):
        if self.path == "/health":
            if ollama_ready():
                self._respond(200, {"status": "ok", "model": MODEL_NAME})
            else:
                self._respond(503, {"status": "initializing"})

        elif self.path.startswith("/v1/models"):
            try:
                r = urllib.request.urlopen(f"{OLLAMA_BASE}/api/tags", timeout=5)
                data = json.loads(r.read())
                models = [{"id": m["name"], "object": "model"} for m in data.get("models", [])]
                self._respond(200, {"object": "list", "data": models})
            except Exception as e:
                self._respond(500, {"error": str(e)})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        length  = int(self.headers.get("Content-Length", 0))
        body    = self.rfile.read(length) if length else b"{}"

        if self.path == "/v1/chat/completions":
            try:
                req_data  = json.loads(body)
                # Always use the configured MODEL_NAME regardless of what the
                # client sends — Ollama model names don't match HuggingFace names.
                requested_model = req_data.get("model", MODEL_NAME)
                ollama_model = MODEL_NAME

                # Convert OpenAI format → Ollama format
                ollama_req = {
                    "model":    ollama_model,
                    "messages": req_data.get("messages", []),
                    "stream":   req_data.get("stream", False),
                    "options": {
                        "temperature": req_data.get("temperature", 0.7),
                        "num_predict": req_data.get("max_tokens", 512),
                    },
                }
                payload = json.dumps(ollama_req).encode()
                req = urllib.request.Request(
                    f"{OLLAMA_BASE}/api/chat",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                resp = urllib.request.urlopen(req, timeout=120)
                data = json.loads(resp.read())

                # Convert Ollama response → OpenAI format
                openai_resp = {
                    "id":      "chatcmpl-gpud",
                    "object":  "chat.completion",
                    "model":   requested_model,
                    "choices": [{
                        "index": 0,
                        "message": data.get("message", {}),
                        "finish_reason": "stop",
                    }],
                    "usage": data.get("usage", {}),
                }
                self._respond(200, openai_resp)
            except urllib.error.HTTPError as e:
                # Read Ollama's actual error body instead of generic message
                try:
                    err_body = json.loads(e.read().decode())
                    self._respond(e.code, err_body)
                except Exception:
                    self._respond(e.code, {"error": f"Ollama error: {e.code} {e.reason}"})
            except Exception as e:
                self._respond(500, {"error": str(e)})
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), ProxyHandler)
    print(f"[gpud-ollama] health proxy listening on :{PORT}")
    server.serve_forever()
