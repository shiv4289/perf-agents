"""Run one load sweep (several concurrency levels) against the deployed config.

Two interchangeable backends, chosen with `loadgen` in target.toml [sweep]:

  "builtin"           bench/loadgen.py. Stdlib only, fast to start, good for CI
                      and quick tries.
  "mlperf-endpoints"  MLCommons `inference-endpoint` (MLPerf Endpoints), the
                      industry-standard client: realistic datasets, MLPerf
                      early-stopping percentiles, a multi-process HTTP client.
                      Install with `make mlperf-setup`.

Both backends write the SAME files, so agents, prompts and evals don't care
which one ran:

  results/<config>/summary.json   one entry per concurrency level
  results/<config>/requests.csv   one row per request

The mlperf-endpoints backend also keeps every raw MLPerf report under
results/<config>/mlperf_endpoints/c<N>/ for audit or a later submission.
"""
from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .config import ROOT

BACKENDS = ("builtin", "mlperf-endpoints")
CSV_FIELDS = ["concurrency", "ok", "ttft_ms", "itl_ms", "e2e_ms", "tokens", "error"]


class SweepError(RuntimeError):
    """The load generator failed. The message is shown to the agent."""


def run_sweep(target, sweep: dict, slo_ttft_ms: float, out_dir: Path) -> str:
    """Run every level in sweep["levels"]; write summary.json and requests.csv. Returns a one-line summary."""
    backend = sweep.get("loadgen", "builtin")
    if backend == "builtin":
        return _run_builtin(target, sweep, slo_ttft_ms, out_dir)
    if backend == "mlperf-endpoints":
        return _run_mlperf_endpoints(target, sweep, slo_ttft_ms, out_dir)
    raise SweepError(f"unknown loadgen {backend!r}; choose one of {BACKENDS}")


# ------------------------------------------------------------------ builtin

def _run_builtin(target, sweep, slo_ttft_ms, out_dir) -> str:
    cmd = [sys.executable, str(ROOT / "bench" / "loadgen.py"),
           "--url", target.url, "--model", target.model,
           "--levels", sweep["levels"], "--duration", str(sweep["duration_s"]),
           "--warmup", str(sweep["warmup_s"]), "--max-tokens", str(sweep["max_tokens"]),
           "--slo-ttft-ms", str(slo_ttft_ms), "--out", str(out_dir)]
    if target.api_key_env:
        cmd += ["--api-key-env", target.api_key_env]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if proc.returncode != 0:
        raise SweepError(f"load sweep failed: {proc.stderr[-1500:]}")
    lines = proc.stdout.strip().splitlines()
    return lines[-1] if lines else "(no output)"


# --------------------------------------------------------- mlperf-endpoints

def find_inference_endpoint(cfg: dict) -> str:
    """Path to the `inference-endpoint` CLI: target.toml setting, then ./.venv-mlperf, then PATH."""
    candidates = [cfg.get("command") or "", str(ROOT / ".venv-mlperf" / "bin" / "inference-endpoint"),
                  "inference-endpoint"]
    for c in candidates:
        if c and (shutil.which(c) or Path(c).is_file()):
            return c
    raise SweepError("MLCommons inference-endpoint is not installed. Run `make mlperf-setup`, "
                     "or set [sweep.mlperf_endpoints] command in target.toml.")


def _run_mlperf_endpoints(target, sweep, slo_ttft_ms, out_dir: Path) -> str:
    cfg = sweep.get("mlperf_endpoints", {})
    binary = find_inference_endpoint(cfg)
    dataset = ROOT / cfg.get("dataset", "bench/datasets/mixed_prompts.jsonl")
    # Endpoints counts tokens client-side (TPS, TPOT). The mock has its own tiny tokenizer;
    # real models default to their own name, which Endpoints resolves on Hugging Face.
    tokenizer = cfg.get("tokenizer") or (str(ROOT / "bench" / "mock_tokenizer")
                                         if target.model == "bench" else target.model)
    # Endpoints' auto (-1) starts ~10 worker processes, which can't even start up on a
    # small VM or CI runner. There, use 1 worker; on real load-generator hosts keep auto.
    small_host = (os.cpu_count() or 1) <= 4
    workers = int(cfg.get("workers", -1))
    if workers == -1 and small_host:
        workers = 1
    # NUMA-aware CPU pinning helps on big load-generator hosts; on a few cores it only slows startup.
    affinity = ["--no-cpu-affinity"] if small_host else []
    out_dir.mkdir(parents=True, exist_ok=True)

    levels, rows = [], []
    for conc in [int(x) for x in sweep["levels"].split(",")]:
        report = out_dir / "mlperf_endpoints" / f"c{conc}"
        if report.exists():
            shutil.rmtree(report)
        cmd = [binary, "benchmark", "online",
               "--endpoints", target.url, "--model", target.model, "--tokenizer", tokenizer,
               "--dataset", str(dataset), "--api-type", cfg.get("api_type", "openai"),
               "--load-pattern", "concurrency", "--concurrency", str(conc),
               # Issue for duration_s, then let in-flight requests finish. num-samples is just
               # "more than we'll ever issue"; the duration cap is what ends the run.
               "--num-samples", "10000000",
               "--runtime.max-issue-duration-ms", str(int(sweep["duration_s"] * 1000)),
               "--max-output-tokens", str(sweep["max_tokens"]),
               "--min-output-tokens", str(sweep["max_tokens"]),
               "--workers", str(workers),
               "--timeout", str(int(sweep["duration_s"] * 10 + 300)),
               "--report-dir", str(report), *affinity, *cfg.get("extra_args", [])]
        if target.api_key_env and os.environ.get(target.api_key_env):
            cmd += ["--api-key", os.environ[target.api_key_env]]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=int(sweep["duration_s"] * 10 + 600))
        summary_file = report / "performance" / "result_summary.json"
        if proc.returncode != 0 or not summary_file.is_file():
            tail = "\n".join(l for l in (proc.stderr + proc.stdout).splitlines() if "ERROR" in l)[-1500:]
            hint = (" Hint: lower [sweep.mlperf_endpoints] workers in target.toml."
                    if "Workers failed to initialize" in tail else "")
            raise SweepError(f"inference-endpoint failed at concurrency {conc} "
                             f"(exit {proc.returncode}): {tail or 'no result_summary.json written'}.{hint}")

        level, level_rows = _convert(json.loads(summary_file.read_text()),
                                     report / "events.jsonl", conc, slo_ttft_ms)
        levels.append(level)
        rows.extend(level_rows)

    (out_dir / "summary.json").write_text(json.dumps(
        {"slo_ttft_ms": slo_ttft_ms, "max_tokens": sweep["max_tokens"],
         "loadgen": "mlperf-endpoints", "levels": levels}, indent=2))
    with open(out_dir / "requests.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return json.dumps(levels[-1])


def _ms(stat: dict | None, pct: str) -> float | None:
    """A percentile from result_summary.json (nanoseconds) in milliseconds."""
    if not stat or stat.get("percentiles", {}).get(pct) is None:
        return None
    return round(stat["percentiles"][pct] / 1e6, 1)


def _convert(rs: dict, events_path: Path, conc: int, slo_ttft_ms: float) -> tuple[dict, list[dict]]:
    """Map one MLPerf Endpoints run onto our summary.json level and requests.csv rows."""
    # Per-request timings from the event log: issued -> first chunk (TTFT) -> complete (E2E).
    issued, first, done, failed = {}, {}, {}, set()
    if events_path.is_file():
        for line in events_path.open():
            e = json.loads(line)
            uid, kind, t = e.get("sample_uuid"), e.get("event_type", ""), e.get("timestamp_ns")
            if not uid:
                continue
            if kind == "sample.issued":
                issued[uid] = t
            elif kind == "sample.recv_first":
                first.setdefault(uid, t)
            elif kind == "sample.complete":
                done[uid] = t
            elif kind.startswith("error."):
                failed.add(uid)

    rows = []
    for uid, t0 in issued.items():
        ok = uid in done and uid in first and uid not in failed
        rows.append({"concurrency": conc, "ok": ok,
                     "ttft_ms": round((first[uid] - t0) / 1e6, 3) if uid in first else "",
                     "itl_ms": "", "e2e_ms": round((done[uid] - t0) / 1e6, 3) if uid in done else "",
                     "tokens": "", "error": "" if ok else "failed"})

    wall_s = (rs.get("duration_ns") or 0) / 1e9
    met = sum(1 for r in rows if r["ok"] and r["ttft_ms"] != "" and r["ttft_ms"] <= slo_ttft_ms)
    es = (rs.get("ttft") or {}).get("early_stopping_percentiles", {}).get("99.0")
    level = {
        "concurrency": conc,
        "requests": rs.get("n_samples_issued", len(rows)),
        "errors": (rs.get("n_samples_failed") or 0) + (rs.get("n_samples_dropped") or 0),
        "ttft_p50_ms": _ms(rs.get("ttft"), "50.0"),
        "ttft_p99_ms": _ms(rs.get("ttft"), "99.0"),
        # TPOT (time per output token) is MLPerf's name for what we call ITL.
        "itl_p50_ms": _ms(rs.get("tpot"), "50.0"),
        "itl_p99_ms": _ms(rs.get("tpot"), "99.0"),
        "output_tokens_per_s": round(rs.get("tps") or 0, 1),
        "requests_per_s": round(rs.get("qps") or 0, 2),
        "goodput_rps": round(met / wall_s, 2) if wall_s else 0,
        "slo_attainment_pct": round(100 * met / len(rows), 1) if rows else 0,
        # MLPerf extras. ttft_p99_early_stopping_ms is MLPerf's conservative p99 estimate
        # (null when there are too few samples for a confident estimate).
        "ttft_p99_early_stopping_ms": round(es / 1e6, 1) if es is not None else None,
        "interactivity_tokens_per_s_per_user": round(rs.get("e2e_avg_interactivity") or 0, 1),
        "complete": bool(rs.get("complete")),
    }
    return level, rows
