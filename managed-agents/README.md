# Agentic perf testing on Claude Managed Agents

> One of two versions in [perf-agents](../README.md). This one runs the agents on **Claude Managed Agents**. For your own model endpoints and evals, see the [root README](../README.md).

> One of two versions in [perf-agents](../README.md). This one runs the agents on **Claude Managed Agents**. For your own model endpoints and evals, see the [root README](../README.md).

A planner agent runs a closed-loop performance search over inference serving
configs. It delegates to three specialists and stops when the frontier converges
or the run budget runs out.

```
perf-lead (planner, Opus)
 ├─ executor  (Haiku)  deploy config, drive load sweep
 ├─ observer  (Haiku)  clean numbers, recompute p99s, flag bad data
 └─ analyst   (Haiku)  knee per config, frontier, convergence, report
```

All four share one sandbox. Each specialist works in its own isolated thread,
so the planner's context stays small.

## Layout

| Path | What it is |
|---|---|
| `agents/*.md` | Agent definitions (model, tools, prompt), versioned in git |
| `environment.yaml` | Cloud sandbox config |
| `bench/mock_server.py` | Simulated inference server with a realistic knee and OOMs |
| `bench/loadgen.py` | Stdlib streaming load generator, writes per-request CSV and a summary |
| `run.py` | Starts a session, streams progress, downloads the outputs |

## Run it

```bash
brew install anthropics/tap/ant      # or the Linux release binary
pip install anthropic
export ANTHROPIC_API_KEY=...

cd perf-agents
ant apply agents/perf-lead.md agents/executor.md agents/observer.md agents/analyst.md environment.yaml
export PERF_LEAD_AGENT_ID=agent_...  PERF_ENV_ID=env_...   # IDs printed by ant apply

python3 run.py --budget 8 --slo-ttft-ms 500
```

Outputs land in `outputs/<session_id>/`: `report.md`, `experiments.json`,
`frontier.csv`, plus raw `requests.csv` and `summary.json` per config.

Test the bench locally first, without any agents:

```bash
python3 bench/mock_server.py --port 8000 &
curl -XPOST localhost:8000/admin/config -d '{"tp":4,"max_batch":128}'
python3 bench/loadgen.py --url http://127.0.0.1:8000 --levels 8,32,64,128 --duration 5 --out /tmp/r1
```

## Pointing it at real NAI

The mock stands in for a real endpoint. To test a real NAI cluster:

- **Sandbox location:** NAI is on-prem, so a cloud sandbox can't reach it.
  Use a self-hosted sandbox inside the cluster network (see "Self-hosted
  sandboxes" in the Managed Agents docs). The agent definitions don't change.
- **Deploy step:** replace the executor's `curl /admin/config` with NAI's
  endpoint API or `kubectl` (update the InferenceService/vLLM args, wait for ready).
- **Load generator:** `loadgen.py` speaks OpenAI-style streaming, so it works
  against a vLLM endpoint as is. Swap in guidellm or `vllm bench serve` later.
- **Observer:** scrape Prometheus (KV cache %, queue depth, GPU utilization)
  instead of the mock's `/metrics`.
- **Credentials:** store NAI tokens in a vault and pass `vault_ids` at session
  creation, rather than putting them in prompts.

## Guardrails to add before scaling

- A session budget at creation, to cap token spend.
- The run budget is enforced only by the prompt today. For real GPUs, add a
  hard cap in the executor (for example, a lock file counting runs).
- Run each config at least twice before the analyst flags a regression,
  so the agents don't chase noise.
