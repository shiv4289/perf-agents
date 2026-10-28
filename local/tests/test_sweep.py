"""Unit tests for the MLPerf Endpoints adapter (perfagents/sweep.py). No Endpoints install needed.

The fixtures mirror the files `inference-endpoint benchmark online` writes:
performance/result_summary.json (times in nanoseconds) and events.jsonl.
"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import perfagents.sweep as sweep_mod
from perfagents.sweep import SweepError, _convert, run_sweep

MS = 1_000_000   # nanoseconds per millisecond


def stat(p50_ms, p99_ms, es99_ms=None):
    return {"percentiles": {"50.0": p50_ms * MS, "99.0": p99_ms * MS},
            "early_stopping_percentiles": {"99.0": es99_ms * MS if es99_ms else None}}


RESULT_SUMMARY = {
    "n_samples_issued": 3, "n_samples_completed": 2, "n_samples_failed": 1, "n_samples_dropped": 0,
    "duration_ns": 2_000 * MS, "complete": True, "qps": 1.0, "tps": 64.0,
    "e2e_avg_interactivity": 40.0,
    "ttft": stat(50, 400, es99_ms=450), "tpot": stat(20, 30),
}


def write_events(path: Path):
    """Three requests: a fast one, a slow one (misses a 300 ms SLO), and one that errors."""
    ev = [("a", "sample.issued", 0), ("a", "sample.recv_first", 50), ("a", "sample.complete", 900),
          ("b", "sample.issued", 0), ("b", "sample.recv_first", 400), ("b", "sample.complete", 1200),
          ("c", "sample.issued", 0), ("c", "error.client", 100)]
    with open(path, "w") as f:
        f.write(json.dumps({"event_type": "session.started", "timestamp_ns": 0, "sample_uuid": ""}) + "\n")
        for uid, kind, t_ms in ev:
            f.write(json.dumps({"event_type": kind, "timestamp_ns": t_ms * MS, "sample_uuid": uid}) + "\n")


class ConvertTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = Path(self.tmp.name) / "events.jsonl"
        write_events(self.events)
        self.level, self.rows = _convert(RESULT_SUMMARY, self.events, conc=8, slo_ttft_ms=300)

    def tearDown(self):
        self.tmp.cleanup()

    def test_nanoseconds_become_milliseconds(self):
        self.assertEqual((self.level["ttft_p50_ms"], self.level["ttft_p99_ms"]), (50.0, 400.0))
        self.assertEqual((self.level["itl_p50_ms"], self.level["itl_p99_ms"]), (20.0, 30.0))   # TPOT -> ITL
        self.assertEqual(self.level["ttft_p99_early_stopping_ms"], 450.0)

    def test_throughput_and_errors_carry_over(self):
        self.assertEqual(self.level["output_tokens_per_s"], 64.0)
        self.assertEqual(self.level["errors"], 1)
        self.assertEqual(self.level["concurrency"], 8)

    def test_per_request_rows_and_slo_from_events(self):
        ok = {r["ttft_ms"] for r in self.rows if r["ok"]}
        self.assertEqual(ok, {50.0, 400.0})
        self.assertEqual(sum(not r["ok"] for r in self.rows), 1)          # the errored request
        self.assertEqual(self.level["slo_attainment_pct"], round(100 / 3, 1))   # only "a" met 300 ms
        self.assertEqual(self.level["goodput_rps"], 0.5)                   # 1 request in 2 s

    def test_same_keys_as_builtin_loadgen(self):
        builtin_keys = {"concurrency", "requests", "errors", "ttft_p50_ms", "ttft_p99_ms",
                        "itl_p50_ms", "itl_p99_ms", "output_tokens_per_s", "requests_per_s",
                        "goodput_rps", "slo_attainment_pct"}
        self.assertTrue(builtin_keys <= set(self.level))


class BackendSelectionTests(unittest.TestCase):
    def test_unknown_backend_is_a_clear_error(self):
        with self.assertRaises(SweepError):
            run_sweep(target=None, sweep={"loadgen": "nope"}, slo_ttft_ms=500, out_dir=Path("."))

    def test_missing_install_says_how_to_fix(self):
        sweep = {"loadgen": "mlperf-endpoints", "levels": "4", "duration_s": 1, "max_tokens": 8,
                 "mlperf_endpoints": {"command": "/nonexistent/inference-endpoint"}}

        class T:
            url, model, api_key_env = "http://x", "m", ""
        orig_root = sweep_mod.ROOT
        sweep_mod.ROOT = Path(tempfile.mkdtemp())          # no ./.venv-mlperf here
        try:
            if shutil.which("inference-endpoint"):
                self.skipTest("inference-endpoint is on PATH")
            with self.assertRaisesRegex(SweepError, "make mlperf-setup"):
                run_sweep(T(), sweep, 500, Path(tempfile.mkdtemp()))
        finally:
            sweep_mod.ROOT = orig_root


if __name__ == "__main__":
    unittest.main()
