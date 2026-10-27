#!/usr/bin/env python3
"""Streaming load generator for OpenAI-style /v1/completions endpoints.

Sweeps concurrency levels and writes:
  - <out>/requests.csv : one row per request (raw data, recompute percentiles later)
  - <out>/summary.json : per-level TTFT/ITL percentiles, tokens/s, goodput, errors

Usage:
  python3 loadgen.py --url http://127.0.0.1:8000 --levels 4,8,16,32 \
      --duration 15 --max-tokens 64 --slo-ttft-ms 500 --out results/tp4_bs128
Stdlib only, so it runs in any sandbox.
"""
import argparse
import csv
import json
import os
import statistics
import threading
import time
import urllib.request


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


MODEL = "bench"   # overridden by --model (real endpoints need the served model name)
HEADERS = {"Content-Type": "application/json"}   # --api-key-env adds Authorization


def one_request(url, max_tokens, timeout):
    """Send one streaming completion; measure TTFT (first token) and ITL (gap between tokens)."""
    body = json.dumps({"model": MODEL, "prompt": "hello", "max_tokens": max_tokens, "stream": True}).encode()
    req = urllib.request.Request(f"{url}/v1/completions", data=body, headers=HEADERS)
    start = time.perf_counter()
    first, last, tokens = None, None, 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            for raw in resp:
                line = raw.decode().strip()
                if not line.startswith("data:"):
                    continue
                if line == "data: [DONE]":
                    break
                now = time.perf_counter()
                if first is None:
                    first = now
                last = now
                tokens += 1
        if first is None:
            return {"ok": False, "error": "no tokens"}
        ttft = (first - start) * 1000
        itl = ((last - first) * 1000 / (tokens - 1)) if tokens > 1 else 0.0
        return {"ok": True, "ttft_ms": ttft, "itl_ms": itl, "e2e_ms": (last - start) * 1000, "tokens": tokens}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": type(e).__name__}


def run_level(url, concurrency, duration, max_tokens, timeout, warmup):
    """Keep `concurrency` requests in flight for warmup+duration seconds.
    Requests sent during warmup are discarded, so we measure steady state only."""
    results, lock = [], threading.Lock()
    t0 = time.perf_counter()
    stop_at = t0 + warmup + duration

    def worker():
        while time.perf_counter() < stop_at:
            sent = time.perf_counter()
            r = one_request(url, max_tokens, timeout)
            if sent - t0 >= warmup:
                r["concurrency"] = concurrency
                with lock:
                    results.append(r)

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0 - warmup
    return results, wall


def summarize(results, wall, concurrency, slo_ttft):
    ok = [r for r in results if r["ok"]]
    ttft = [r["ttft_ms"] for r in ok]
    itl = [r["itl_ms"] for r in ok]
    tokens = sum(r["tokens"] for r in ok)
    met = [r for r in ok if r["ttft_ms"] <= slo_ttft]
    return {
        "concurrency": concurrency,
        "requests": len(results),
        "errors": len(results) - len(ok),
        "ttft_p50_ms": pct(ttft, 50),
        "ttft_p99_ms": pct(ttft, 99),
        "itl_p50_ms": pct(itl, 50),
        "itl_p99_ms": pct(itl, 99),
        "output_tokens_per_s": round(tokens / wall, 1) if wall > 0 else 0,
        "requests_per_s": round(len(ok) / wall, 2) if wall > 0 else 0,
        "goodput_rps": round(len(met) / wall, 2) if wall > 0 else 0,
        "slo_attainment_pct": round(100 * len(met) / len(results), 1) if results else 0,
        "mean_itl_ms": round(statistics.mean(itl), 1) if itl else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--levels", default="4,8,16,32")
    ap.add_argument("--duration", type=float, default=15)
    ap.add_argument("--warmup", type=float, default=2)
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--timeout", type=float, default=60)
    ap.add_argument("--slo-ttft-ms", type=float, default=500)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="bench", help="model name sent in each request")
    ap.add_argument("--api-key-env", default="", help="env var holding a bearer token")
    args = ap.parse_args()

    global MODEL
    MODEL = args.model
    if args.api_key_env and os.environ.get(args.api_key_env):
        HEADERS["Authorization"] = f"Bearer {os.environ[args.api_key_env]}"

    os.makedirs(args.out, exist_ok=True)
    levels = [int(x) for x in args.levels.split(",")]
    summary, rows = [], []
    for c in levels:
        res, wall = run_level(args.url, c, args.duration, args.max_tokens, args.timeout, args.warmup)
        s = summarize(res, wall, c, args.slo_ttft_ms)
        summary.append(s)
        rows.extend(res)
        print(json.dumps(s), flush=True)

    fields = ["concurrency", "ok", "ttft_ms", "itl_ms", "e2e_ms", "tokens", "error"]
    with open(os.path.join(args.out, "requests.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(args.out, "summary.json"), "w") as f:
        json.dump({"slo_ttft_ms": args.slo_ttft_ms, "max_tokens": args.max_tokens, "levels": summary}, f, indent=2)


if __name__ == "__main__":
    main()
