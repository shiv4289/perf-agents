"""The inference server under test, behind one small interface.

    with make_target(cfg) as target:
        ok, msg = target.deploy(tp=4, max_batch=128)
        target.url            # where the load generator sends requests
        target.metrics_text() # Prometheus-style metrics snapshot

Two implementations:
  MockTarget     starts bench/mock_server.py locally (no GPUs needed)
  CommandTarget  runs a shell command you provide to redeploy a real endpoint
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from .config import ROOT


def _http(method: str, url: str, body: dict | None = None, timeout: float = 30) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        return 0, str(e)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockTarget:
    """Runs the simulated inference server in a child process for one run."""
    model = "bench"
    api_key_env = ""

    def __init__(self):
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.proc = None

    def __enter__(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "bench" / "mock_server.py"), "--port", str(self.port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):                       # wait up to ~5 s for it to come up
            if _http("GET", f"{self.url}/health", timeout=1)[0] == 200:
                return self
            time.sleep(0.1)
        raise RuntimeError("mock server did not start")

    def __exit__(self, *exc):
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)

    def deploy(self, tp: int, max_batch: int) -> tuple[bool, str]:
        status, text = _http("POST", f"{self.url}/admin/config", {"tp": tp, "max_batch": max_batch})
        return status == 200, text

    def metrics_text(self) -> str:
        return _http("GET", f"{self.url}/metrics")[1]


class CommandTarget:
    """A real endpoint. Redeploys by running `deploy_command` from target.toml."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.url = cfg["url"].rstrip("/")
        self.model = cfg.get("model", "bench")
        self.api_key_env = cfg.get("api_key_env", "")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def deploy(self, tp: int, max_batch: int) -> tuple[bool, str]:
        cmd = self.cfg["deploy_command"].format(tp=tp, max_batch=max_batch)
        proc = subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True, text=True,
                              timeout=self.cfg.get("ready_timeout_s", 900))
        if proc.returncode != 0:
            return False, f"deploy command failed ({proc.returncode}): {(proc.stderr or proc.stdout)[-1500:]}"
        # Wait until the new deployment answers.
        deadline = time.time() + self.cfg.get("ready_timeout_s", 900)
        while time.time() < deadline:
            if _http("GET", self.cfg["ready_url"], timeout=5)[0] == 200:
                return True, json.dumps({"deployed": {"tp": tp, "max_batch": max_batch}})
            time.sleep(5)
        return False, "deployment did not become ready before ready_timeout_s"

    def metrics_text(self) -> str:
        url = self.cfg.get("metrics_url")
        return _http("GET", url)[1] if url else "(no metrics_url configured)"


def make_target(target_cfg: dict):
    if target_cfg.get("type", "mock") == "mock":
        return MockTarget()
    return CommandTarget(target_cfg["command"])
