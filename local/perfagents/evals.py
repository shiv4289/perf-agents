"""Evals: run the same perf test with different model line-ups and compare them.

    python3 -m perfagents eval                         # evals/suite.toml
    python3 -m perfagents eval --only suggested,all-lightning --repeats 1
    python3 -m perfagents eval --judge                 # also grade report.md with the judge model

For every (scenario × candidate × repeat) it runs a full perf test, then scores it:

  quality       score of the recommended config ÷ score of the true best config
                (1.0 = found the best; uses the oracle's numbers, not the agents')
  hit           quality >= 0.98, i.e. found the best or something within 2% of it
  knee_ok       reported max safe concurrency matches the oracle's knee for that config
  deliverables  share of the 4 output files present and valid (0-1)
  runs_used     deploys used (budget is enforced in code, so this is efficiency)
  failed_deploys, duplicate_deploys   wasted runs
  tokens, llm_calls, tool_errors, max_turn_hits, wall_s
  judge         optional 1-5 average from the judge model on report.md

Outputs go to evals/results/<timestamp>/: results.csv (one row per run),
leaderboard.md (averages per candidate and per scenario) and the run folders.
"""
from __future__ import annotations

import csv
import json
import statistics
import tomllib
from datetime import datetime
from pathlib import Path

from . import llm
from .config import ROOT, Scenario, load_roles, load_target, merge_sweep
from .oracle import best_config, build_oracle, score_config
from .team import run_perf_test

SUITE_PATH = ROOT / "evals" / "suite.toml"
FIELDS = ["scenario", "candidate", "repeat", "planner", "executor", "observer", "analyst",
          "recommended", "oracle_best", "quality", "hit", "knee_ok", "deliverables",
          "runs_used", "budget", "failed_deploys", "duplicate_deploys", "guardrail_blocks",
          "tokens", "llm_calls", "tool_errors", "max_turn_hits", "llm_errors", "wall_s", "judge",
          "notes"]


# ----------------------------------------------------------------- scoring

def _load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def score_run(meta: dict, workspace: Path, oracle: dict, scenario: Scenario) -> dict:
    notes = []
    best_name, best_score = best_config(oracle, scenario.slo_ttft_ms, scenario.objective)

    # --- Did it recommend a real, tested config, and how good is it? ---
    rec = _load_json(workspace / "recommendation.json")
    rec_name = rec.get("config") if isinstance(rec, dict) else None
    tested_ok = {d["config"] for d in meta["deploy_log"] if d["status"] == "deployed"}
    quality, knee_ok = 0.0, False
    if not rec_name:
        notes.append("no valid recommendation.json")
    elif rec_name not in tested_ok:
        notes.append(f"recommended {rec_name} but never tested it successfully")
    elif rec_name not in oracle["configs"]:
        notes.append(f"recommended unknown config {rec_name}")
    else:
        rec_score, rec_knee = score_config(oracle["configs"][rec_name], scenario.slo_ttft_ms,
                                           scenario.objective)
        quality = rec_score / best_score if best_score else 0.0
        knee_ok = rec.get("max_safe_concurrency") == rec_knee
        if not knee_ok:
            notes.append(f"knee {rec.get('max_safe_concurrency')} vs oracle {rec_knee}")

    # --- Deliverables present and valid ---
    report, frontier = workspace / "report.md", workspace / "frontier.csv"
    frontier_header = frontier.read_text().partition("\n")[0] if frontier.is_file() else ""
    checks = [
        isinstance(rec, dict) and bool(rec_name),                        # recommendation.json
        isinstance(_load_json(workspace / "experiments.json"), list),    # experiments.json
        report.is_file() and report.stat().st_size > 200,                # report.md, not a stub
        "config" in frontier_header,                                     # frontier.csv with header
    ]

    # --- Efficiency ---
    counted = [d["config"] for d in meta["deploy_log"] if d["counted"]]
    totals = meta["totals"]
    return {
        "recommended": rec_name or "",
        "oracle_best": best_name,
        "quality": round(quality, 3),
        "hit": int(quality >= 0.98),
        "knee_ok": int(knee_ok),
        "deliverables": round(sum(map(bool, checks)) / len(checks), 2),
        "runs_used": meta["runs_used"],
        "budget": scenario.budget,
        "failed_deploys": sum(1 for d in meta["deploy_log"] if d["status"] == "failed"),
        "duplicate_deploys": len(counted) - len(set(counted)),
        "guardrail_blocks": meta["guardrail_blocks"],
        "tokens": totals.get("prompt_tokens", 0) + totals.get("completion_tokens", 0),
        "llm_calls": totals.get("llm_calls", 0),
        "tool_errors": totals.get("tool_errors", 0),
        "max_turn_hits": totals.get("max_turn_hits", 0),
        "llm_errors": totals.get("llm_errors", 0),
        "wall_s": meta["wall_s"],
        "notes": "; ".join(notes),
    }


def judge_report(judge_role, workspace: Path, oracle: dict, meta: dict) -> float | str:
    """Ask the judge model to grade report.md against the measured numbers. Returns 1-5 or ''."""
    report = workspace / "report.md"
    if not report.is_file():
        return ""
    tested = {d["config"] for d in meta["deploy_log"] if d["status"] in ("deployed", "failed")}
    truth = {name: oracle["configs"][name] for name in tested if name in oracle["configs"]}
    messages = [{"role": "system", "content": judge_role.prompt},
                {"role": "user", "content": f"REPORT:\n{report.read_text()[:12000]}\n\n"
                                            f"MEASURED RESULTS:\n{json.dumps(truth)[:20000]}"}]
    try:
        reply, _ = llm.chat(judge_role.model, messages)
        text = (reply.get("content") or "").strip()
        grades = json.loads(text[text.find("{"): text.rfind("}") + 1])
        nums = [float(grades[k]) for k in ("accuracy", "reasoning", "completeness", "clarity")]
        (workspace / "judge.json").write_text(json.dumps(grades, indent=2))
        return round(statistics.mean(nums), 2)
    except (llm.LLMError, ValueError, KeyError, TypeError) as e:
        (workspace / "judge.json").write_text(json.dumps({"error": str(e)}))
        return ""


# ---------------------------------------------------------------- reporting

def _mean(rows, key):
    vals = [float(r[key]) for r in rows if r[key] not in ("", None)]
    return round(statistics.mean(vals), 2) if vals else ""


def write_leaderboard(rows: list[dict], path: Path, judge: bool):
    cols = ["quality", "hit", "knee_ok", "deliverables", "runs_used", "tokens",
            "tool_errors", "wall_s"] + (["judge"] if judge else [])
    lines = ["# Eval leaderboard", "",
             "Higher is better for quality, hit, knee_ok, deliverables and judge. "
             "Lower is better for runs_used, tokens, tool_errors and wall_s. "
             "Values are means over all runs.", ""]

    def table(groups: dict[str, list[dict]], first_col: str):
        out = [f"| {first_col} | runs | " + " | ".join(cols) + " |",
               "|---|---|" + "---|" * len(cols)]
        ranked = sorted(groups.items(), key=lambda kv: (-(_mean(kv[1], "quality") or 0),
                                                        _mean(kv[1], "tokens") or 0))
        for name, grp in ranked:
            out.append(f"| {name} | {len(grp)} | " + " | ".join(str(_mean(grp, c)) for c in cols) + " |")
        return out

    by_cand = {}
    for r in rows:
        by_cand.setdefault(r["candidate"], []).append(r)
    lines += ["## Overall, by candidate", ""] + table(by_cand, "candidate") + [""]

    lines += ["## Model line-ups", "", "| candidate | planner | executor | observer | analyst |",
              "|---|---|---|---|---|"]
    for name, grp in by_cand.items():
        r = grp[0]
        lines.append(f"| {name} | {r['planner']} | {r['executor']} | {r['observer']} | {r['analyst']} |")
    lines.append("")

    for scen in sorted({r["scenario"] for r in rows}):
        grp = {}
        for r in rows:
            if r["scenario"] == scen:
                grp.setdefault(r["candidate"], []).append(r)
        true_best = next(r["oracle_best"] for r in rows if r["scenario"] == scen)
        lines += [f"## Scenario: {scen} (true best: {true_best})", ""] + table(grp, "candidate") + [""]

    problems = [r for r in rows if r["notes"]]
    if problems:
        lines += ["## Notes from individual runs", ""]
        lines += [f"- {r['scenario']} / {r['candidate']} / r{r['repeat']}: {r['notes']}" for r in problems]
    path.write_text("\n".join(lines) + "\n")


# --------------------------------------------------------------- main entry

def run_suite(only: list[str] | None = None, repeats: int | None = None,
              judge: bool = False, judge_model: str | None = None, verbose: bool = False,
              loadgen: str | None = None) -> Path:
    suite = tomllib.loads(SUITE_PATH.read_text())
    settings = suite.get("settings", {})
    repeats = repeats or int(settings.get("repeats", 1))
    target_cfg = load_target()
    if "sweep" in settings:                      # evals can use a shorter sweep than normal runs
        target_cfg["sweep"] = merge_sweep(target_cfg["sweep"], settings["sweep"])
    if loadgen:                                  # --loadgen beats both target.toml and the suite
        target_cfg["sweep"]["loadgen"] = loadgen
    print(f"Load generator: {target_cfg['sweep'].get('loadgen', 'builtin')}")

    candidates = suite["candidates"]
    if only:
        candidates = [c for c in candidates if c["name"] in only]
        if not candidates:
            raise SystemExit(f"No candidates named {only} in {SUITE_PATH}")

    print("Building or loading the oracle (brute-force answer key)...")
    oracle = build_oracle(target_cfg, verbose=verbose)

    out_dir = ROOT / "evals" / "results" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True)
    csv_path = out_dir / "results.csv"
    rows = []
    judge_role = load_roles({"judge": judge_model} if judge_model else None)["judge"] if judge else None

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for scen in suite["scenarios"]:
            scenario = Scenario(**scen)
            for cand in candidates:
                overrides = {k: v for k, v in cand.items() if k != "name"}
                roles = load_roles(overrides)
                for rep in range(1, repeats + 1):
                    run_id = f"{scenario.name}__{cand['name']}__r{rep}"
                    print(f"\n=== {run_id} ===")
                    meta = run_perf_test(roles, scenario, target_cfg, runs_dir=out_dir / "runs",
                                         run_id=run_id, verbose=verbose)
                    ws = out_dir / "runs" / run_id
                    row = {"scenario": scenario.name, "candidate": cand["name"], "repeat": rep,
                           **meta["models"], **score_run(meta, ws, oracle, scenario)}
                    row["judge"] = judge_report(judge_role, ws, oracle, meta) if judge else ""
                    if judge and row["judge"] == "":
                        row["notes"] = "; ".join(filter(None, [row["notes"], "judge failed (see judge.json)"]))
                    writer.writerow(row)
                    f.flush()
                    rows.append(row)
                    print(f"    quality={row['quality']} hit={row['hit']} runs={row['runs_used']}/"
                          f"{row['budget']} tokens={row['tokens']} {row['notes']}")

    write_leaderboard(rows, out_dir / "leaderboard.md", judge)
    print(f"\nLeaderboard: {out_dir / 'leaderboard.md'}\nRaw rows:    {csv_path}")
    return out_dir
