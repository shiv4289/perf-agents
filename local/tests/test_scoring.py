"""Unit tests for the eval scorer and the tool guardrails. No LLM, no network.

Run:  python3 -m unittest discover -s tests -v
"""
import json
import tempfile
import unittest
from pathlib import Path

from perfagents.config import Scenario
from perfagents.evals import score_run
from perfagents.oracle import best_config, score_config
from perfagents.tools import RunContext, call_tool


def level(c, p99, tps, errors=0):
    return {"concurrency": c, "ttft_p99_ms": p99, "output_tokens_per_s": tps, "errors": errors}


# A tiny hand-made oracle: tp8 wins on total throughput, tp4 wins per GPU.
ORACLE = {"configs": {
    "tp4_bs128": {"tp": 4, "max_batch": 128, "status": "ok",
                  "levels": [level(16, 50, 400), level(64, 120, 1600), level(128, 900, 1700)]},
    "tp8_bs128": {"tp": 8, "max_batch": 128, "status": "ok",
                  "levels": [level(16, 40, 500), level(64, 90, 2400), level(128, 400, 3000)]},
    "tp2_bs64": {"tp": 2, "max_batch": 64, "status": "failed"},
}}


def meta_for(deployed, failed=()):
    log = [{"config": c, "status": "deployed", "counted": True} for c in deployed]
    log += [{"config": c, "status": "failed", "counted": True} for c in failed]
    return {"deploy_log": log, "runs_used": len(log), "guardrail_blocks": 0, "wall_s": 1.0,
            "totals": {"prompt_tokens": 10, "completion_tokens": 5}}


class OracleTests(unittest.TestCase):
    def test_slo_excludes_slow_levels(self):
        # c=128 has p99 900 ms > 500 ms SLO, so the score comes from c=64
        self.assertEqual(score_config(ORACLE["configs"]["tp4_bs128"], 500, "throughput"), (1600, 64))

    def test_objectives_pick_different_winners(self):
        self.assertEqual(best_config(ORACLE, 500, "throughput")[0], "tp8_bs128")        # 3000 vs 1600
        self.assertEqual(best_config(ORACLE, 500, "throughput_per_gpu")[0], "tp4_bs128")  # 400 vs 375

    def test_failed_config_scores_zero(self):
        self.assertEqual(score_config(ORACLE["configs"]["tp2_bs64"], 500, "throughput"), (0.0, None))


class ScoreRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.scenario = Scenario(slo_ttft_ms=500, objective="throughput")

    def tearDown(self):
        self.tmp.cleanup()

    def recommend(self, config, knee):
        (self.ws / "recommendation.json").write_text(
            json.dumps({"config": config, "max_safe_concurrency": knee}))

    def test_best_config_scores_one(self):
        self.recommend("tp8_bs128", 128)
        s = score_run(meta_for(["tp4_bs128", "tp8_bs128"]), self.ws, ORACLE, self.scenario)
        self.assertEqual((s["quality"], s["hit"], s["knee_ok"]), (1.0, 1, 1))

    def test_worse_config_gets_partial_quality(self):
        self.recommend("tp4_bs128", 64)
        s = score_run(meta_for(["tp4_bs128", "tp8_bs128"]), self.ws, ORACLE, self.scenario)
        self.assertAlmostEqual(s["quality"], round(1600 / 3000, 3))
        self.assertEqual(s["hit"], 0)

    def test_untested_recommendation_scores_zero(self):
        self.recommend("tp8_bs128", 128)              # recommended but never deployed
        s = score_run(meta_for(["tp4_bs128"]), self.ws, ORACLE, self.scenario)
        self.assertEqual(s["quality"], 0.0)
        self.assertIn("never tested", s["notes"])

    def test_missing_files_lower_deliverables(self):
        self.recommend("tp8_bs128", 128)              # only 1 of 4 deliverables present
        s = score_run(meta_for(["tp8_bs128"]), self.ws, ORACLE, self.scenario)
        self.assertEqual(s["deliverables"], 0.25)


class FakeTarget:
    url, model, api_key_env = "http://unused", "bench", ""

    def deploy(self, tp, max_batch):
        return True, "ok"


class GuardrailTests(unittest.TestCase):
    def make_ctx(self, budget):
        return RunContext(workspace=Path(tempfile.mkdtemp()), target=FakeTarget(),
                          scenario=Scenario(budget=budget),
                          search_space={"tp": [4, 8], "max_batch": [128]}, sweep={})

    def deploy(self, ctx, tp, bs):
        return call_tool(ctx, ["deploy_config"], "deploy_config",
                         json.dumps({"tp": tp, "max_batch": bs}))

    def test_budget_is_enforced(self):
        ctx = self.make_ctx(budget=1)
        self.assertFalse(self.deploy(ctx, 4, 128)[1])
        out, is_error = self.deploy(ctx, 8, 128)
        self.assertTrue(is_error)
        self.assertIn("budget", out)
        self.assertEqual(ctx.runs_used, 1)

    def test_out_of_space_is_rejected_and_not_counted(self):
        ctx = self.make_ctx(budget=5)
        out, is_error = self.deploy(ctx, 2, 128)
        self.assertTrue(is_error)
        self.assertEqual(ctx.runs_used, 0)

    def test_sweep_requires_deployed_config(self):
        ctx = self.make_ctx(budget=5)
        out, is_error = call_tool(ctx, ["run_load_sweep"], "run_load_sweep",
                                  json.dumps({"config_name": "tp4_bs128"}))
        self.assertTrue(is_error)

    def test_role_cannot_use_other_tools(self):
        ctx = self.make_ctx(budget=5)
        out, is_error = call_tool(ctx, ["read_file"], "deploy_config", "{}")
        self.assertTrue(is_error)

    def test_bad_json_is_reported_not_raised(self):
        ctx = self.make_ctx(budget=5)
        out, is_error = call_tool(ctx, ["deploy_config"], "deploy_config", "{tp: 4")
        self.assertTrue(is_error)
        self.assertIn("JSON", out)

    def test_files_cannot_escape_workspace(self):
        ctx = self.make_ctx(budget=5)
        out, is_error = call_tool(ctx, ["read_file"], "read_file", json.dumps({"path": "../../etc/passwd"}))
        self.assertTrue(is_error)


if __name__ == "__main__":
    unittest.main()
