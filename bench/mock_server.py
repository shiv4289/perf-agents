#!/usr/bin/env python3
"""Simulated LLM inference server (OpenAI-style streaming /v1/completions).

Lets the perf agents run end to end without real GPUs. Latency degrades
realistically with concurrency, and a config is "deployed" via
POST /admin/config {"tp": 4, "max_batch": 128}. Oversized configs fail with
an OOM error, like a KV cache that doesn't fit.

Run:  python3 mock_server.py --port 8000
"""
import argparse
import json
import math
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"tp": 4, "max_batch": 64, "active": 0, "served": 0}
LOCK = threading.Lock()


# Pretend we serve a ~70B model on 80 GB GPUs.
GPU_MEM_GB = 80
WEIGHTS_GB = 140          # split across the tp GPUs
KV_GB_PER_SEQ = 0.6       # KV cache needed per concurrent sequence


def is_oom(tp: int, max_batch: int) -> bool:
    """Weights take memory first; whatever is left must hold max_batch sequences of KV cache.
    tp=1 never fits (140 GB of weights on one 80 GB GPU), like a real 70B model."""
    kv_free_gb = tp * GPU_MEM_GB - WEIGHTS_GB
    return kv_free_gb <= 0 or max_batch * KV_GB_PER_SEQ > kv_free_gb


def latency_model(tp: int, max_batch: int, active: int):
    """Return (ttft_ms, itl_ms) for the current load.

    More GPUs (tp) make each token faster, but GPUs must talk to each other every
    step, so the gain shrinks (`comm`). That's why the most efficient config PER GPU
    is usually not the biggest one.
    """
    comm = 1 + 0.35 * math.log2(tp)                     # tensor-parallel communication overhead
    prefill = 120 / tp ** 0.7
    itl = 25 / tp ** 0.6 * comm
    capacity = max_batch * 0.35 * tp ** 0.5 / comm      # requests it can batch before queueing
    load = active / capacity
    itl *= 1 + 0.6 * min(load, 1.0)
    queue_wait = max(0.0, active - capacity) / capacity * 900
    jitter = random.uniform(0.9, 1.15)
    return (prefill + queue_wait) * jitter, itl * jitter


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True})
        if self.path == "/admin/config":
            return self._json(200, {k: STATE[k] for k in ("tp", "max_batch")})
        if self.path == "/metrics":
            with LOCK:
                text = (
                    f"vllm:num_requests_running {STATE['active']}\n"
                    f"vllm:requests_served_total {STATE['served']}\n"
                    f"mock:tensor_parallel_size {STATE['tp']}\n"
                    f"mock:max_num_seqs {STATE['max_batch']}\n"
                )
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/admin/config":
            cfg = self._body()
            tp, mb = int(cfg.get("tp", 1)), int(cfg.get("max_batch", 64))
            if tp not in (1, 2, 4, 8):
                return self._json(400, {"error": "tp must be 1, 2, 4 or 8"})
            if is_oom(tp, mb):
                return self._json(507, {"error": f"OOM: KV cache for max_batch={mb} does not fit at tp={tp}"})
            with LOCK:
                STATE.update(tp=tp, max_batch=mb)
            time.sleep(1.0)
            return self._json(200, {"deployed": {"tp": tp, "max_batch": mb}})

        if self.path == "/v1/completions":
            req = self._body()
            max_tokens = int(req.get("max_tokens", 64))
            with LOCK:
                STATE["active"] += 1
                active, tp, mb = STATE["active"], STATE["tp"], STATE["max_batch"]
            try:
                ttft, itl = latency_model(tp, mb, active)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                time.sleep(ttft / 1000)
                for i in range(max_tokens):
                    chunk = {"choices": [{"text": f" t{i}", "index": 0}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(itl / 1000)
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with LOCK:
                    STATE["active"] -= 1
                    STATE["served"] += 1
            return
        self._json(404, {"error": "not found"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 1024


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    print(f"mock inference server on :{args.port}", flush=True)
    Server(("127.0.0.1", args.port), Handler).serve_forever()
