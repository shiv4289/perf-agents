"""Wire the four agents together and run one complete perf test.

The planner is the only agent the user talks to. Its `delegate` tool starts a
specialist (executor / observer / analyst) on a sub-task and returns the
specialist's report, like a foreman handing jobs to workers.

Each run gets its own folder, runs/<run_id>/, which holds everything:
results/ from the load sweeps, the agents' deliverables, transcript.jsonl and
run_meta.json (models used, tokens, deploys, timing).
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from .agent import Tracker, run_agent
from .config import ROOT, RoleSpec, Scenario
from .target import make_target
from .tools import RunContext

SPECIALISTS = ("executor", "observer", "analyst")


def goal_message(scenario: Scenario, search_space: dict, sweep: dict) -> str:
    """The task given to the planner. Everything it needs is stated explicitly."""
    objective = {
        "throughput": "the highest SLO-compliant output tokens/s",
        "throughput_per_gpu": "the highest SLO-compliant output tokens/s PER GPU "
                              "(tokens/s divided by tp, since tp = number of GPUs)",
    }[scenario.objective]
    return f"""Run an automated performance test.

SLO: p99 TTFT <= {scenario.slo_ttft_ms} ms.
Objective: find the config with {objective}.
Search space: tp in {search_space['tp']}, max_batch in {search_space['max_batch']}.
Run budget: at most {scenario.budget} deploys, failed deploys included. Code enforces this.
Load sweep per config (fixed, run by the executor): concurrency levels {sweep['levels']}.
Config names look like tp4_bs128.

Deliverables, all in the workspace root:
- recommendation.json: {{"config": "<name>", "max_safe_concurrency": <int>,
  "slo_compliant_tokens_per_s": <number>, "reason": "<one sentence>"}}
  The recommended config MUST be one you actually tested successfully.
- experiments.json: a list with one entry per config tried:
  {{"config", "status", "why_chosen"}}
- report.md and frontier.csv, written by the analyst."""


def run_perf_test(roles: dict[str, RoleSpec], scenario: Scenario, target_cfg: dict,
                  runs_dir: Path | None = None, run_id: str | None = None,
                  verbose: bool = True) -> dict:
    """Run one full perf test and return a summary dict (also saved as run_meta.json)."""
    run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    workspace = (runs_dir or ROOT / "runs") / run_id
    workspace.mkdir(parents=True, exist_ok=True)
    tracker = Tracker(workspace / "transcript.jsonl", verbose=verbose)

    started = time.time()
    with make_target(target_cfg) as target:
        ctx = RunContext(workspace=workspace, target=target, scenario=scenario,
                         search_space=target_cfg["search_space"], sweep=target_cfg["sweep"])

        def delegate(agent: str, task: str) -> str:
            if agent not in SPECIALISTS:
                return f"Unknown agent {agent!r}; choose one of {SPECIALISTS}"
            return run_agent(roles[agent], task, ctx, tracker, depth=1)

        ctx.delegate = delegate
        tracker.say(f"Run {run_id}  scenario={scenario.name}  workspace={workspace}")
        final = run_agent(roles["planner"], goal_message(scenario, ctx.search_space, ctx.sweep),
                          ctx, tracker)

    meta = {
        "run_id": run_id,
        "scenario": vars(scenario),
        "models": {r: roles[r].model.name for r in ("planner", *SPECIALISTS)},
        "wall_s": round(time.time() - started, 1),
        "runs_used": ctx.runs_used,
        "guardrail_blocks": ctx.guardrail_blocks,
        "deploy_log": ctx.deploy_log,
        "totals": tracker.totals(),
        "by_role": {r: dict(c) for r, c in tracker.by_role.items()},
        "planner_final": final,
    }
    (workspace / "run_meta.json").write_text(json.dumps(meta, indent=2))
    tracker.say(f"\nDone in {meta['wall_s']}s. Deploys used: {ctx.runs_used}/{scenario.budget}. "
                f"Outputs in {workspace}")
    return meta
