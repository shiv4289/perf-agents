"""Command-line entry point.  Run `python3 -m perfagents --help`.

Commands:
  check-models   ping each endpoint and verify it returns real tool calls
  run            run one perf test with the configured (or overridden) models
  oracle         build the brute-force answer key used by evals
  eval           compare model line-ups from evals/suite.toml
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from . import evals, llm
from .config import Scenario, load_models, load_roles, load_target, parse_overrides
from .oracle import build_oracle
from .team import run_perf_test


def cmd_check_models(args):
    """Send each model a tiny request that should produce one tool call.

    A model can be reachable and still useless for agents: if vLLM runs without
    --enable-auto-tool-choice or with the wrong --tool-call-parser, the tool
    call comes back as plain text. This catches that before a long run.
    """
    models = load_models()
    if args.models:
        names = args.models.split(",")
    else:   # default: only the models the roles actually use
        names = sorted({r.model.name for r in load_roles().values()})
    tool = [{"type": "function", "function": {
        "name": "deploy_config", "description": "Deploy a serving config.",
        "parameters": {"type": "object", "properties": {"tp": {"type": "integer"},
                                                        "max_batch": {"type": "integer"}},
                       "required": ["tp", "max_batch"]}}}]
    msgs = [{"role": "user", "content": "Deploy tp=4 and max_batch=128 using the tool."}]

    print(f"{'model':<22}{'status':<10}{'latency':>9}  detail")
    all_ok = True
    for name in names:
        if name not in models:
            print(f"{name:<22}{'UNKNOWN':<10}{'':>9}  not in models.toml")
            all_ok = False
            continue
        t0 = time.time()
        try:
            reply, usage = llm.chat(models[name], msgs, tool, timeout_s=60, retries=1)
        except llm.LLMError as e:
            print(f"{name:<22}{'DOWN':<10}{'':>9}  {str(e)[:90]}")
            all_ok = False
            continue
        dt = f"{time.time() - t0:.1f}s"
        calls = reply.get("tool_calls") or []
        if calls and calls[0]["function"]["name"] == "deploy_config":
            try:
                args_ok = json.loads(calls[0]["function"]["arguments"]) == {"tp": 4, "max_batch": 128}
            except json.JSONDecodeError:
                args_ok = False
            status = "OK" if args_ok else "ARGS?"
            detail = calls[0]["function"]["arguments"]
        else:
            status, detail = "NO-TOOL", ("returned text instead of a tool call; check vLLM "
                                         "--enable-auto-tool-choice / --tool-call-parser: "
                                         + str(reply.get("content"))[:60])
        all_ok &= status == "OK"
        print(f"{name:<22}{status:<10}{dt:>9}  {detail}")
    raise SystemExit(0 if all_ok else 1)


def cmd_run(args):
    overrides = parse_overrides(args.model)
    if args.all_models:                         # e.g. --all-models fake for offline testing
        overrides = {r: args.all_models for r in ("planner", "executor", "observer", "analyst")}
    roles = load_roles(overrides)
    scenario = Scenario(name="cli", slo_ttft_ms=args.slo_ttft_ms, budget=args.budget,
                        objective=args.objective)
    target_cfg = load_target()
    if args.duration:                           # shorter sweeps for quick tries
        target_cfg["sweep"]["duration_s"] = args.duration
    meta = run_perf_test(roles, scenario, target_cfg,
                         runs_dir=Path(args.runs_dir) if args.runs_dir else None)
    print("\nPlanner summary:\n" + meta["planner_final"])


def cmd_oracle(args):
    oracle = build_oracle(load_target(), rebuild=args.rebuild)
    print(f"Oracle has {len(oracle['configs'])} configs; saved to {oracle['path']}")


def cmd_eval(args):
    if args.suite:
        evals.SUITE_PATH = Path(args.suite).resolve()
    evals.run_suite(only=args.only.split(",") if args.only else None,
                    repeats=args.repeats, judge=args.judge or bool(args.judge_model),
                    judge_model=args.judge_model, verbose=args.verbose)


def main():
    ap = argparse.ArgumentParser(prog="perfagents", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check-models", help="verify endpoints return proper tool calls")
    p.add_argument("--models", help="comma-separated model names (default: those used in roles.toml)")
    p.set_defaults(fn=cmd_check_models)

    p = sub.add_parser("run", help="run one perf test")
    p.add_argument("--model", action="append", metavar="ROLE=MODEL",
                   help="override a role's model, e.g. --model planner=muse-spark (repeatable)")
    p.add_argument("--all-models", metavar="MODEL", help="use one model for every role")
    p.add_argument("--slo-ttft-ms", type=float, default=500)
    p.add_argument("--budget", type=int, default=8, help="max deploys, failed ones included")
    p.add_argument("--objective", choices=["throughput", "throughput_per_gpu"], default="throughput")
    p.add_argument("--duration", type=float, help="seconds per sweep level (overrides target.toml)")
    p.add_argument("--runs-dir", help="where run folders go (default: runs/)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("oracle", help="build the brute-force answer key for evals")
    p.add_argument("--rebuild", action="store_true")
    p.set_defaults(fn=cmd_oracle)

    p = sub.add_parser("eval", help="compare model line-ups")
    p.add_argument("--suite", help="suite file (default: evals/suite.toml)")
    p.add_argument("--only", help="comma-separated candidate names to run")
    p.add_argument("--repeats", type=int, help="override repeats from the suite")
    p.add_argument("--judge", action="store_true", help="grade report.md with the judge model")
    p.add_argument("--judge-model", help="use this model as judge instead of roles.toml's (implies --judge)")
    p.add_argument("--verbose", action="store_true", help="print every agent step")
    p.set_defaults(fn=cmd_eval)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
