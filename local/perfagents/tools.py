"""Tools the agents can call, and the guardrails around them.

Each tool is a plain Python function `fn(ctx, **args) -> str` plus a JSON
schema the model sees. roles.toml decides which role gets which tools.

Guardrails live HERE, in code, not in prompts:
  * deploy_config refuses configs outside the search space
  * deploy_config refuses once the run budget is used up
  * run_load_sweep only runs against the config that is actually deployed
  * file tools can't touch anything outside the run's workspace folder
So even a weak or confused model can't burn extra GPU time.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Scenario
from .sweep import SweepError, run_sweep

MAX_TOOL_OUTPUT = 6000          # characters; long outputs are cut so context stays small
CONFIG_NAME = re.compile(r"^tp(\d+)_bs(\d+)$")


@dataclass
class RunContext:
    """Shared state for one perf-test run. Every tool receives it."""
    workspace: Path                    # runs/<run_id>/ ; all agent files live here
    target: object                     # MockTarget or CommandTarget
    scenario: Scenario
    search_space: dict                 # {"tp": [...], "max_batch": [...]}
    sweep: dict                        # levels, duration_s, warmup_s, max_tokens
    deployed: str | None = None        # config name currently live, e.g. "tp4_bs128"
    deploy_log: list = field(default_factory=list)   # every deploy attempt, in order
    guardrail_blocks: int = 0          # times a tool refused an action
    delegate: Callable | None = None   # set by team.py; runs a specialist agent

    @property
    def runs_used(self) -> int:
        return sum(1 for d in self.deploy_log if d["counted"])


class ToolError(Exception):
    """Raised by a tool to send an error message back to the model."""


# ---------------------------------------------------------------- file tools

def _safe_path(ctx: RunContext, path: str) -> Path:
    """Resolve `path` inside the workspace; refuse anything that escapes it."""
    p = (ctx.workspace / path.lstrip("/")).resolve()
    if ctx.workspace.resolve() not in (p, *p.parents):
        raise ToolError(f"path {path!r} is outside the workspace")
    return p


def read_file(ctx, path: str) -> str:
    p = _safe_path(ctx, path)
    if not p.is_file():
        raise ToolError(f"no such file: {path}")
    return p.read_text(errors="replace")


def write_file(ctx, path: str, content: str) -> str:
    p = _safe_path(ctx, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {len(content)} chars to {path}"


def list_files(ctx, path: str = ".") -> str:
    p = _safe_path(ctx, path)
    if not p.exists():
        raise ToolError(f"no such directory: {path}")
    files = sorted(str(f.relative_to(ctx.workspace)) for f in p.rglob("*")
                   if f.is_file() and ".scratch" not in f.parts)
    return "\n".join(files) or "(empty)"


def run_python(ctx, code: str, timeout_s: int = 60) -> str:
    """Run a Python script with the workspace as its working directory."""
    scratch = ctx.workspace / ".scratch"
    scratch.mkdir(exist_ok=True)
    script = scratch / f"script_{time.time_ns()}.py"
    script.write_text(code)
    try:
        proc = subprocess.run([sys.executable, "-I", str(script)], cwd=ctx.workspace,
                              capture_output=True, text=True, timeout=min(timeout_s, 300))
    except subprocess.TimeoutExpired:
        raise ToolError(f"script timed out after {timeout_s}s")
    out = f"exit code {proc.returncode}\n--- stdout ---\n{proc.stdout}"
    if proc.stderr:
        out += f"\n--- stderr ---\n{proc.stderr}"
    return out


# ------------------------------------------------------------ executor tools

def deploy_config(ctx, tp: int, max_batch: int) -> str:
    try:                                   # some models send "4" instead of 4
        tp, max_batch = int(tp), int(max_batch)
    except (TypeError, ValueError):
        raise ToolError("tp and max_batch must be integers")
    name = f"tp{tp}_bs{max_batch}"
    space = ctx.search_space

    # Guardrail 1: stay inside the search space.
    if tp not in space["tp"] or max_batch not in space["max_batch"]:
        ctx.guardrail_blocks += 1
        ctx.deploy_log.append({"config": name, "status": "rejected_out_of_space", "counted": False})
        raise ToolError(f"{name} is outside the search space {space}. Not deployed.")

    # Guardrail 2: hard run budget. Failed deploys count too (they cost GPU time).
    if ctx.runs_used >= ctx.scenario.budget:
        ctx.guardrail_blocks += 1
        ctx.deploy_log.append({"config": name, "status": "rejected_budget", "counted": False})
        raise ToolError(f"Run budget of {ctx.scenario.budget} is used up. Not deployed. "
                        "Report what you have.")

    ok, msg = ctx.target.deploy(tp, max_batch)
    ctx.deploy_log.append({"config": name, "tp": tp, "max_batch": max_batch,
                           "status": "deployed" if ok else "failed", "detail": msg[:300],
                           "counted": True, "t": time.time()})
    ctx.deployed = name if ok else None
    left = ctx.scenario.budget - ctx.runs_used
    if not ok:
        return f"DEPLOY FAILED for {name}: {msg}. Runs left in budget: {left}."
    return f"Deployed {name}. Runs left in budget: {left}."


def run_load_sweep(ctx, config_name: str) -> str:
    # Guardrail 3: measure only what is actually deployed, so results can't be mislabeled.
    if config_name != ctx.deployed:
        ctx.guardrail_blocks += 1
        raise ToolError(f"{config_name!r} is not the deployed config (deployed: {ctx.deployed}). "
                        "Deploy it first.")
    out_dir = ctx.workspace / "results" / config_name
    try:     # builtin loadgen or MLPerf Endpoints, per target.toml [sweep] loadgen
        last = run_sweep(ctx.target, ctx.sweep, ctx.scenario.slo_ttft_ms, out_dir)
    except SweepError as e:
        raise ToolError(str(e))
    return (f"Results written to results/{config_name}/ (summary.json, requests.csv).\n"
            f"Highest level: {last}")


def snapshot_metrics(ctx, config_name: str) -> str:
    out = ctx.workspace / "results" / config_name / "metrics.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    text = ctx.target.metrics_text()
    out.write_text(text)
    return f"Saved results/{config_name}/metrics.txt:\n{text}"


# ------------------------------------------------------------- planner tool

def delegate(ctx, agent: str, task: str) -> str:
    """Hand a task to a specialist. team.py wires ctx.delegate to the real runner."""
    if ctx.delegate is None:
        raise ToolError("delegation is not available for this role")
    return ctx.delegate(agent, task)


# ----------------------------------------------------------------- registry
# name -> (function, description shown to the model, JSON schema of arguments)

def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


REGISTRY: dict[str, tuple[Callable, str, dict]] = {
    "read_file": (read_file, "Read a text file from the workspace.",
                  _schema({"path": {"type": "string"}}, ["path"])),
    "write_file": (write_file, "Write a text file in the workspace (creates folders).",
                   _schema({"path": {"type": "string"}, "content": {"type": "string"}},
                           ["path", "content"])),
    "list_files": (list_files, "List files under a workspace folder.",
                   _schema({"path": {"type": "string", "default": "."}}, [])),
    "run_python": (run_python, "Run a Python 3 script. Working directory is the workspace. "
                               "Print what you want to see.",
                   _schema({"code": {"type": "string"},
                            "timeout_s": {"type": "integer", "default": 60}}, ["code"])),
    "deploy_config": (deploy_config, "Deploy a serving config to the inference server. "
                                     "Counts against the run budget, even if it fails.",
                      _schema({"tp": {"type": "integer"}, "max_batch": {"type": "integer"}},
                              ["tp", "max_batch"])),
    "run_load_sweep": (run_load_sweep, "Run the standard load sweep against the deployed config.",
                       _schema({"config_name": {"type": "string",
                                                "description": "e.g. tp4_bs128"}},
                               ["config_name"])),
    "snapshot_metrics": (snapshot_metrics, "Save the server's metrics for a config.",
                         _schema({"config_name": {"type": "string"}}, ["config_name"])),
    "delegate": (delegate, "Give a task to a specialist agent and get its report back. "
                           "Each call starts the specialist fresh, so include everything it needs.",
                 _schema({"agent": {"type": "string", "enum": ["executor", "observer", "analyst"]},
                          "task": {"type": "string"}}, ["agent", "task"])),
}


def schemas_for(tool_names: list[str]) -> list[dict]:
    """The `tools` list sent to the model for a role."""
    return [{"type": "function",
             "function": {"name": n, "description": REGISTRY[n][1], "parameters": REGISTRY[n][2]}}
            for n in tool_names]


def call_tool(ctx: RunContext, allowed: list[str], name: str, raw_args: str) -> tuple[str, bool]:
    """Run one tool call from the model. Returns (output_text, is_error).

    Errors are returned to the model as text instead of crashing the run, so
    the model gets a chance to correct itself (bad JSON, unknown tool, ...).
    """
    if name not in allowed:
        return f"ERROR: tool {name!r} is not available to you. Available: {allowed}", True
    try:
        args = json.loads(raw_args) if raw_args.strip() else {}
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except (json.JSONDecodeError, ValueError) as e:
        return f"ERROR: could not parse tool arguments as JSON ({e}). Got: {raw_args[:300]}", True
    try:
        out = REGISTRY[name][0](ctx, **args)
        return out[:MAX_TOOL_OUTPUT] + ("\n...[truncated]" if len(out) > MAX_TOOL_OUTPUT else ""), False
    except ToolError as e:
        return f"ERROR: {e}", True
    except TypeError as e:            # wrong or missing argument names
        return f"ERROR: bad arguments for {name}: {e}", True
