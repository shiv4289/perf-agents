#!/usr/bin/env python3
"""A scripted, OpenAI-compatible fake LLM for testing the plumbing offline.

It is NOT smart. Each role follows a fixed script, worked out from the
conversation so far, but it uses the real tools, the real mock server and
real numbers. So `make smoke` exercises everything except model judgment:
config loading, the agent loop, tool calls, guardrails, delegation, oracle
and eval scoring.

Run:  python3 tests/fake_llm.py --port 8599
"""
import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PLAN = [(4, 128), (2, 64), (4, 256), (8, 256)]       # (2, 64) OOMs on the mock: tests failure handling

OBSERVER_SCRIPT = r'''
import json, csv, pathlib
d = pathlib.Path("{folder}")
s = json.loads((d / "summary.json").read_text())
flags = [f"c={{lv['concurrency']}}: {{lv['errors']}} errors" for lv in s["levels"] if lv["errors"]]
(d / "observed.json").write_text(json.dumps({{"levels": s["levels"], "flags": flags}}, indent=2))
for lv in s["levels"]:
    print(lv["concurrency"], lv["ttft_p99_ms"], lv["output_tokens_per_s"])
print("flags:", flags)
'''

ANALYST_SCRIPT = r'''
import json, pathlib
slo, objective = {slo}, "{objective}"
rows = []
for d in sorted(pathlib.Path("results").iterdir()):
    tp, bs = (int(x) for x in d.name[2:].split("_bs"))
    lv = [l for l in json.loads((d / "summary.json").read_text())["levels"]
          if l["errors"] == 0 and l["ttft_p99_ms"] is not None and l["ttft_p99_ms"] <= slo]
    if not lv:
        continue
    best = max(lv, key=lambda l: l["output_tokens_per_s"])
    score = best["output_tokens_per_s"] / (tp if objective == "throughput_per_gpu" else 1)
    knee = max(lv, key=lambda l: l["concurrency"])
    rows.append((d.name, tp, bs, round(score, 1), knee["concurrency"], knee["ttft_p99_ms"]))
rows.sort(key=lambda r: -r[3])
lines = ["config,tp,max_batch,score,knee_concurrency,p99_ttft_at_knee_ms,on_frontier"]
lines += [",".join(map(str, r)) + f",{{i == 0}}" for i, r in enumerate(rows)]
pathlib.Path("frontier.csv").write_text("\n".join(lines) + "\n")
report = ["# Perf test report (fake analyst)", "", f"Recommended: **{{rows[0][0]}}**", "",
          "| config | score | knee | p99 TTFT at knee |", "|---|---|---|---|"]
report += [f"| {{r[0]}} | {{r[3]}} | {{r[4]}} | {{r[5]}} |" for r in rows]
report += ["", "Failed configs: see experiments.json. Data quality: see observed.json in each folder."]
pathlib.Path("report.md").write_text("\n".join(report) + "\n")
print(f"RECOMMEND {{rows[0][0]}} KNEE {{rows[0][4]}} SCORE {{rows[0][3]}}")
'''


def tool_call(name, args):
    return {"role": "assistant", "content": None, "tool_calls": [
        {"id": f"call_{name}_{abs(hash(json.dumps(args))) % 10**8}", "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}


def text(content):
    return {"role": "assistant", "content": content}


def history(messages):
    """[(tool_name, args_dict, result_text)] for every tool call made so far."""
    results = {m["tool_call_id"]: m["content"] for m in messages if m["role"] == "tool"}
    out = []
    for m in messages:
        for tc in m.get("tool_calls") or []:
            out.append((tc["function"]["name"], json.loads(tc["function"]["arguments"]),
                        results.get(tc["id"], "")))
    return out


def planner(messages):
    goal = messages[1]["content"]
    slo = float(re.search(r"p99 TTFT <= ([\d.]+)", goal).group(1))
    objective = "throughput_per_gpu" if "PER GPU" in goal else "throughput"
    done = history(messages)
    tasks = [(a.get("agent"), a.get("task", ""), r) for n, a, r in done if n == "delegate"]

    for tp, bs in PLAN:
        name = f"tp{tp}_bs{bs}"
        ex = [r for ag, t, r in tasks if ag == "executor" and name in t]
        if not ex:
            return tool_call("delegate", {"agent": "executor",
                                          "task": f"Deploy tp={tp} max_batch={bs}, config name {name}."})
        failed = "FAILED" in ex[0] or "ERROR" in ex[0] or "rejected" in ex[0]
        if not failed and not any(ag == "observer" and name in t for ag, t, _ in tasks):
            return tool_call("delegate", {"agent": "observer", "task": f"Check results/{name}/ SLO={slo}"})

    final = [r for ag, t, r in tasks if ag == "analyst"]
    if not final:
        return tool_call("delegate", {"agent": "analyst",
                                      "task": f"FINAL REPORT. SLO={slo} objective={objective}"})
    written = [a["path"] for n, a, _ in done if n == "write_file"]
    if "recommendation.json" not in written:
        m = re.search(r"RECOMMEND (\S+) KNEE (\d+) SCORE ([\d.]+)", final[0])
        rec = ({"config": m.group(1), "max_safe_concurrency": int(m.group(2)),
                "slo_compliant_tokens_per_s": float(m.group(3)), "reason": "best score (fake)"}
               if m else {"config": None})
        return tool_call("write_file", {"path": "recommendation.json", "content": json.dumps(rec)})
    if "experiments.json" not in written:
        exps = [{"config": f"tp{tp}_bs{bs}", "status": "tested", "why_chosen": "scripted plan"}
                for tp, bs in PLAN]
        return tool_call("write_file", {"path": "experiments.json", "content": json.dumps(exps)})
    return text("Done. See recommendation.json and report.md.")


def executor(messages):
    task = next(m["content"] for m in messages if m["role"] == "user")
    tp, bs = (int(x) for x in re.search(r"tp=(\d+).*?max_batch=(\d+)", task).groups())
    name, done = f"tp{tp}_bs{bs}", [n for n, _, _ in history(messages)]
    last = history(messages)[-1][2] if done else ""
    if not done:
        return tool_call("deploy_config", {"tp": tp, "max_batch": bs})
    if "deploy_config" == done[-1] and ("FAILED" in last or "ERROR" in last):
        return text(f"{name}: deploy failed: {last}")
    if "run_load_sweep" not in done:
        return tool_call("run_load_sweep", {"config_name": name})
    if "snapshot_metrics" not in done:
        return tool_call("snapshot_metrics", {"config_name": name})
    return text(f"{name}: deployed and swept. Results in results/{name}/")


def observer(messages):
    task = messages[1]["content"]
    folder = re.search(r"(results/\S+?)/", task).group(1)
    done = history(messages)
    if not done:
        return tool_call("run_python", {"code": OBSERVER_SCRIPT.format(folder=folder)})
    return text(done[-1][2][:1500])


def analyst(messages):
    task = messages[1]["content"]
    slo = float(re.search(r"SLO=([\d.]+)", task).group(1))
    objective = re.search(r"objective=(\w+)", task).group(1)
    done = history(messages)
    if not done:
        return tool_call("run_python", {"code": ANALYST_SCRIPT.format(slo=slo, objective=objective)})
    return text(done[-1][2])


def judge(messages):
    return text(json.dumps({"accuracy": 4, "reasoning": 3, "completeness": 4, "clarity": 4,
                            "comment": "fake judge"}))


def respond(body):
    msgs = body["messages"]
    system = msgs[0]["content"] if msgs[0]["role"] == "system" else ""
    if system.startswith("You are the planner"):
        return planner(msgs)
    if system.startswith("You check raw"):
        return observer(msgs)
    if system.startswith("You turn performance"):
        return analyst(msgs)
    if system.startswith("You grade"):
        return judge(msgs)
    return executor(msgs)          # executor, and the check-models probe


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        try:
            msg = respond(body)
        except Exception as e:     # surface script bugs instead of hanging the agent
            msg = text(f"[fake llm error: {type(e).__name__}: {e}]")
        payload = json.dumps({"choices": [{"index": 0, "message": msg, "finish_reason": "stop"}],
                              "usage": {"prompt_tokens": 100 * len(body["messages"]),
                                        "completion_tokens": 50}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8599)
    port = ap.parse_args().port
    print(f"fake LLM on :{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
