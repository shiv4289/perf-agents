"""The "answer key" for evals: brute-force every config once.

To grade an agent team we need to know the true best config. The oracle
deploys EVERY config in the search space, runs the same sweep the agents use,
and saves the numbers to evals/oracles/oracle-<key>.json.

With the mock target this takes a few minutes and is cached. With a real
target it costs real GPU time, so build it once and reuse it.

The agents never see the oracle. Only the scorer reads it.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import ROOT
from .target import make_target

ORACLE_DIR = ROOT / "evals" / "oracles"     # one cached file per sweep/search-space setup


def _cache_key(target_cfg: dict) -> str:
    """Rebuild the oracle whenever anything that changes the answer changes."""
    parts = {"search_space": target_cfg["search_space"], "sweep": target_cfg["sweep"],
             "type": target_cfg.get("type")}
    if target_cfg.get("type", "mock") == "mock":       # mock code changes = new answers
        parts["mock"] = hashlib.sha256((ROOT / "bench" / "mock_server.py").read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def build_oracle(target_cfg: dict, rebuild: bool = False, verbose: bool = True) -> dict:
    key = _cache_key(target_cfg)
    path = ORACLE_DIR / f"oracle-{key}.json"
    if path.exists() and not rebuild:
        return json.loads(path.read_text())

    sweep, space = target_cfg["sweep"], target_cfg["search_space"]
    configs = {}
    with make_target(target_cfg) as target, tempfile.TemporaryDirectory() as tmp:
        for tp in space["tp"]:
            for bs in space["max_batch"]:
                name = f"tp{tp}_bs{bs}"
                ok, msg = target.deploy(tp, bs)
                if not ok:
                    configs[name] = {"tp": tp, "max_batch": bs, "status": "failed", "detail": msg[:200]}
                    if verbose:
                        print(f"  oracle {name}: deploy failed")
                    continue
                out = Path(tmp) / name
                cmd = [sys.executable, str(ROOT / "bench" / "loadgen.py"), "--url", target.url,
                       "--model", target.model, "--levels", sweep["levels"],
                       "--duration", str(sweep["duration_s"]), "--warmup", str(sweep["warmup_s"]),
                       "--max-tokens", str(sweep["max_tokens"]), "--out", str(out)]
                if target.api_key_env:
                    cmd += ["--api-key-env", target.api_key_env]
                subprocess.run(cmd, check=True, capture_output=True)
                levels = json.loads((out / "summary.json").read_text())["levels"]
                configs[name] = {"tp": tp, "max_batch": bs, "status": "ok", "levels": levels}
                if verbose:
                    print(f"  oracle {name}: measured {len(levels)} levels")

    oracle = {"key": key, "path": str(path), "configs": configs}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(oracle, indent=2))
    return oracle


def score_config(entry: dict, slo_ttft_ms: float, objective: str) -> tuple[float, int | None]:
    """(score, knee_concurrency) for one oracle entry, using the same definitions as the analyst."""
    if entry.get("status") != "ok":
        return 0.0, None
    ok = [lv for lv in entry["levels"]
          if lv["errors"] == 0 and lv["ttft_p99_ms"] is not None and lv["ttft_p99_ms"] <= slo_ttft_ms]
    if not ok:
        return 0.0, None
    best = max(lv["output_tokens_per_s"] for lv in ok)
    if objective == "throughput_per_gpu":
        best /= entry["tp"]
    return best, max(lv["concurrency"] for lv in ok)


def best_config(oracle: dict, slo_ttft_ms: float, objective: str) -> tuple[str, float]:
    scored = {name: score_config(e, slo_ttft_ms, objective)[0] for name, e in oracle["configs"].items()}
    name = max(scored, key=scored.get)
    return name, scored[name]
