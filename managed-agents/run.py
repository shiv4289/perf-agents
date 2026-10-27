#!/usr/bin/env python3
"""Kick off one agentic perf-test run on Claude Managed Agents.

Prereqs (see README):
  ant apply agents/perf-lead.md agents/executor.md agents/observer.md agents/analyst.md environment.yaml
  export ANTHROPIC_API_KEY=... PERF_LEAD_AGENT_ID=agent_... PERF_ENV_ID=env_...

Usage:
  python3 run.py                       # defaults: mock server, 8-run budget
  python3 run.py --budget 12 --slo-ttft-ms 400
"""
import argparse
import os
import time
from pathlib import Path

import anthropic

HERE = Path(__file__).parent
BETA = "managed-agents-2026-04-01"


def goal_message(args) -> str:
    return f"""Run an automated performance test.

Target: {args.url} (the local mock inference server; the executor starts it if needed).
SLO: p99 TTFT <= {args.slo_ttft_ms} ms.
Search space: tp in {{1, 2, 4, 8}}, max_batch in {{32, 64, 128, 256}}.
Sweep per config: levels {args.levels}, duration {args.duration}s, max_tokens 64.
Run budget: at most {args.budget} configs, including failed deploys.
Config names look like tp4_bs128.

Goal: find the config with the highest SLO-compliant output tokens/s, its max
safe concurrency, and the frontier. Deliver report.md, experiments.json and
frontier.csv in /mnt/session/outputs/."""


def upload_bench(client):
    resources = []
    for name in ("mock_server.py", "loadgen.py"):
        f = client.files.upload(file=HERE / "bench" / name)
        resources.append({"type": "file", "file_id": f.id, "mount_path": f"/bench/{name}"})
    return resources


def stream(client, session_id, message):
    with client.beta.sessions.events.stream(session_id) as events:
        client.beta.sessions.events.send(
            session_id,
            events=[{"type": "user.message", "content": [{"type": "text", "text": message}]}],
        )
        for event in events:
            match event.type:
                case "agent.message":
                    for block in event.content:
                        if block.type == "text":
                            print(block.text, end="", flush=True)
                case "agent.tool_use":
                    print(f"\n  [tool: {event.name}]", flush=True)
                case "session.thread_created":
                    who = getattr(event, "agent_name", "subagent")
                    print(f"\n  [delegated -> {who}]", flush=True)
                case "agent.thread_message_received":
                    who = getattr(event, "from_agent_name", "subagent")
                    print(f"\n  [report <- {who}]", flush=True)
                case "session.status_idle":
                    print("\n\nRun finished.")
                    break


def download_outputs(client, session_id, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    for attempt in range(5):
        files = list(client.beta.files.list(scope_id=session_id, betas=[BETA]))
        if files:
            break
        time.sleep(3)
    for f in files:
        dest = out_dir / f.filename
        client.files.download(f.id).write_to_file(dest)
        print(f"  saved {dest}")
    if not files:
        print("  no output files yet; list them again in a few seconds")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default=os.environ.get("PERF_LEAD_AGENT_ID"))
    ap.add_argument("--env", default=os.environ.get("PERF_ENV_ID"))
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--slo-ttft-ms", type=int, default=500)
    ap.add_argument("--levels", default="16,32,64,96,128")
    ap.add_argument("--duration", type=int, default=10)
    ap.add_argument("--budget", type=int, default=8)
    ap.add_argument("--out", default=str(HERE / "outputs"))
    args = ap.parse_args()
    if not args.agent or not args.env:
        raise SystemExit("Set PERF_LEAD_AGENT_ID and PERF_ENV_ID (printed by `ant apply`).")

    client = anthropic.Anthropic()
    session = client.beta.sessions.create(
        agent=args.agent,
        environment_id=args.env,
        title="Agentic perf test",
        resources=upload_bench(client),
    )
    print(f"Session: {session.id}\n")
    stream(client, session.id, goal_message(args))
    download_outputs(client, session.id, Path(args.out) / session.id)


if __name__ == "__main__":
    main()
