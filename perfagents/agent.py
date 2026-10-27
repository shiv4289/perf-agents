"""The agent loop: the same pattern every agent harness uses, in ~60 lines.

    1. Send the conversation + tool list to the model.
    2. If the model asks for tools, run them and append the results.
    3. Repeat until the model answers without asking for a tool,
       or max_turns is reached.

Usage numbers and a full transcript are recorded in a Tracker, so evals can
compare models on tokens, tool errors and turns, not only on the final answer.
"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from pathlib import Path

from . import llm
from .config import RoleSpec
from .tools import RunContext, call_tool, schemas_for


class Tracker:
    """Counts what each role did, and appends every step to transcript.jsonl."""

    def __init__(self, transcript_path: Path, verbose: bool = True):
        self.transcript_path = transcript_path
        self.verbose = verbose
        self.lock = threading.Lock()
        self.by_role = defaultdict(lambda: defaultdict(int))   # role -> counter -> value

    def add(self, role: str, **counters):
        with self.lock:
            for k, v in counters.items():
                self.by_role[role][k] += v

    def log(self, role: str, kind: str, **data):
        with self.lock, open(self.transcript_path, "a") as f:
            f.write(json.dumps({"t": round(time.time(), 3), "role": role, "kind": kind, **data}) + "\n")

    def say(self, text: str):
        if self.verbose:
            print(text, flush=True)

    def totals(self) -> dict:
        out = defaultdict(int)
        for counters in self.by_role.values():
            for k, v in counters.items():
                out[k] += v
        return dict(out)


def run_agent(role: RoleSpec, task: str, ctx: RunContext, tracker: Tracker, depth: int = 0) -> str:
    """Run one role on one task until it finishes. Returns its final text answer."""
    indent = "  " * depth
    tools = schemas_for(role.tools)
    messages = [{"role": "system", "content": role.prompt},
                {"role": "user", "content": task}]
    tracker.log(role.name, "task", model=role.model.name, task=task)
    tracker.say(f"{indent}▶ {role.name} ({role.model.name})")

    for turn in range(role.max_turns):
        try:
            reply, usage = llm.chat(role.model, messages, tools or None)
        except llm.LLMError as e:
            tracker.add(role.name, llm_errors=1)
            tracker.log(role.name, "llm_error", error=str(e))
            tracker.say(f"{indent}  ✗ LLM error: {e}")
            return f"[{role.name} failed: {e}]"

        tracker.add(role.name, llm_calls=1, **usage)
        messages.append(reply)
        tracker.log(role.name, "assistant", content=reply.get("content"),
                    tool_calls=reply.get("tool_calls"), usage=usage)

        calls = reply.get("tool_calls") or []
        if not calls:                                   # no tool requested = final answer
            text = (reply.get("content") or "").strip()
            tracker.say(f"{indent}◀ {role.name} done")
            return text or f"[{role.name} returned an empty answer]"

        for tc in calls:
            name, args = tc["function"]["name"], tc["function"]["arguments"]
            tracker.say(f"{indent}  · {name} {args[:120]}")
            output, is_error = call_tool(ctx, role.tools, name, args)
            tracker.add(role.name, tool_calls=1, tool_errors=int(is_error))
            tracker.log(role.name, "tool", name=name, args=args, output=output, error=is_error)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": output})

    tracker.add(role.name, max_turn_hits=1)
    tracker.say(f"{indent}◀ {role.name} hit max_turns={role.max_turns}")
    return f"[{role.name} stopped after {role.max_turns} turns without finishing]"
