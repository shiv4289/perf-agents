# perf-agents / local: agents on your own models

> 📍 You are in **`local/`**: the agents run on **your own model endpoints** (vLLM, NIM,
> Nutanix Enterprise AI...). Evals are included. Want Claude-hosted agents instead?
> See [`../managed-agents/`](../managed-agents/). Repo overview: [`../README.md`](../README.md).

All commands below are run from **this folder** (`cd local`).

- 🧠 **Bring your own models:** each agent can use a different OpenAI-compatible endpoint
- 📊 **Evals built in:** compare model line-ups against a brute-force answer key, and get a leaderboard
- 🛡️ **Guardrails in code:** run budget, search space and workspace limits are enforced by the tools, not the prompts
- 📏 **Credible measurement, optional:** switch the load generator to MLCommons **MLPerf Endpoints** with one line
- 🧪 **Runs anywhere:** a mock inference server and a scripted fake LLM let you try everything without GPUs
- 📦 **Zero dependencies:** Python 3.11+ standard library only (MLPerf Endpoints is an opt-in extra)

---

## The idea in 30 seconds

Think of a car-tuning team at a test track:

| Agent | Track equivalent | What it does | Default model |
|---|---|---|---|
| **planner** | the race engineer | decides which setup to try next, and when to stop | Beam |
| **executor** | the mechanic | puts a setup on the car and drives the test laps | Nemotron 3.5 Lightning |
| **observer** | the data logger | checks the lap data is clean and trustworthy | Nemotron 3.5 Lightning |
| **analyst** | the performance analyst | compares all setups, finds the best, writes the report | Nemotron 3.5 Super |

A "setup" is a serving config: `tp` (GPUs per replica) and `max_batch`.
A "test lap" is a load sweep. It ramps up concurrent requests and measures
TTFT (time to first token), ITL (time between tokens) and throughput.

The planner only talks to the specialists. It never touches the server itself:

```
          ┌────────── delegate ──────────┐
planner ──┼──► executor  deploy + load sweep ──► results/<config>/
          ├──► observer  verify numbers      ──► observed.json
          └──► analyst   knee, frontier      ──► report.md, frontier.csv
planner writes recommendation.json + experiments.json at the end
```

**Why these default models:** the planner runs a long reasoning loop, so it gets
the strongest agentic model. The specialists do mechanical work, so they get
fast, cheap models. The analyst gets a step up because it writes the analysis
code. Change any of these in one line (see below).

---

## Quick start

```bash
# 0. No GPUs or models needed for this step: check that everything works offline
make test        # unit tests: scoring math, guardrails, MLPerf adapter
make smoke       # full pipeline with a scripted fake LLM (~2 min)

# 1. Point the models at your endpoints
$EDITOR config/models.toml          # set base_url for beam, muse-spark, nemotron-*
export NAI_API_KEY=...              # or whatever api_key_env you set

# 2. Verify each endpoint really returns tool calls (catches wrong vLLM flags)
make check

# 3. One perf test (against the built-in mock inference server)
make run
# or with options:
python3 -m perfagents run --model planner=muse-spark --budget 6 --slo-ttft-ms 300

# 4. Compare model line-ups
make eval        # add the judge:  make eval-judge
```

Each run writes a folder `runs/<timestamp>/`. Evals write `evals/results/<timestamp>/leaderboard.md`.

---

## Project layout

```
config/
  models.toml      every LLM endpoint (URL, model name, API key env var)   ← edit first
  roles.toml       which model + tools each agent gets by default
  target.toml      the server UNDER TEST, search space, load sweep settings
prompts/           one system prompt per role (plain markdown, easy to tweak)
perfagents/        the code; read it in this order:
  config.py        loads the TOML files, applies --model overrides
  llm.py           ~80-line client for OpenAI-compatible /chat/completions
  agent.py         the agent loop: call model → run tools → repeat
  tools.py         what agents can do + the guardrails (budget, search space)
  sweep.py         runs a load sweep: builtin loadgen or MLPerf Endpoints, same output files
  team.py          wires planner → specialists, runs one full perf test
  target.py        mock server or real endpoint, behind one interface
  oracle.py        brute-forces every config = the eval answer key
  evals.py         runs the suite, scores runs, writes the leaderboard
  __main__.py      the CLI (python3 -m perfagents --help)
bench/
  mock_server.py   simulated inference server: realistic knee, OOMs, TP overhead
  loadgen.py       builtin streaming load generator (works on real vLLM too)
  datasets/        mixed_prompts.jsonl: short chat + long-context prompts, for MLPerf Endpoints
  mock_tokenizer/  tiny tokenizer so MLPerf Endpoints can count the mock's tokens
evals/suite.toml   scenarios × model line-ups to compare
tests/             unit tests, the fake LLM, and the smoke suite
```

---

## Changing models

**For one run:** use `--model role=model` (repeatable), or `--all-models` for every role:

```bash
python3 -m perfagents run --model planner=muse-spark --model analyst=beam
python3 -m perfagents run --all-models nemotron-super
```

**Permanently:** edit `model = ...` in `config/roles.toml`.

**Add a new model:** add a block to `config/models.toml`, then run
`python3 -m perfagents check-models --models <name>`. Any OpenAI-compatible
endpoint with tool calling works.

The vLLM serve flags that matter for agents:

| Model | Flags |
|---|---|
| Nemotron 3.5 Lightning | `--enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser nemotron_v3` |
| Nemotron 3.5 Super | `--enable-auto-tool-choice --reasoning-parser nemotron_v3` plus the tool parser from its model card |
| Beam, Muse Spark | `--enable-auto-tool-choice` plus the tool parser from the model card |

If `check-models` says `NO-TOOL`, the endpoint is up but tool calling is
misconfigured. This is the most common problem.

---

## Evals: which models make the best agents?

`make eval` runs every **scenario** in `evals/suite.toml` with every
**candidate** line-up, `repeats` times each, then scores each run.

**How scoring works.** Before the first eval, the oracle (`oracle.py`) tests
*every* config once and caches the results. This is the answer key; the agents
never see it. The scorer then compares the agents' pick against the true best:

| Column | Meaning | Better |
|---|---|---|
| `quality` | true score of the recommended config ÷ true score of the best config (oracle numbers, so measurement noise doesn't reward luck) | higher, max 1.0 |
| `hit` | quality ≥ 0.98, i.e. it found the best (or within 2%) | higher |
| `knee_ok` | its stated max safe concurrency matches the oracle's (noisy near the SLO, so read it across repeats) | higher |
| `deliverables` | share of the 4 output files present and valid | higher |
| `runs_used` | deploys used. Each one costs GPU time | lower |
| `failed_deploys`, `duplicate_deploys` | wasted runs (e.g. retrying a config that already OOM'd) | lower |
| `guardrail_blocks` | times the code refused an action (over budget, outside search space) | lower |
| `tokens`, `llm_calls` | cost of the agent reasoning | lower |
| `tool_errors` | malformed or wrong tool calls (a good measure of tool-calling skill) | lower |
| `max_turn_hits` | an agent looped until its turn cap | lower |
| `judge` | optional 1-5 grade of report.md by a separate judge model | higher |

**Scenarios included:**
- `max-throughput`: maximize total SLO-compliant tokens/s, with a budget of 8. The answer is the biggest config.
- `per-gpu-tight-budget`: maximize tokens/s per GPU, with a tighter SLO and only 5 runs. The answer is **not** the biggest config, because the mock models TP communication overhead. A planner that just picks the largest setup fails here.

**Adding a candidate:** append to `evals/suite.toml`, listing only the roles you change:

```toml
[[candidates]]
name    = "super-planner"
planner = "nemotron-super"
```

Run a subset with `python3 -m perfagents eval --only suggested,super-planner --repeats 3`.

**Reading the result.** `leaderboard.md` ranks candidates by quality, then
tokens. Look for the cheapest line-up whose quality and hit rate match the best
one. Then check `notes` at the bottom for *why* runs failed. Every run's full
transcript is in `evals/results/<ts>/runs/<run>/transcript.jsonl`.

**Use repeats ≥ 3 before you conclude anything.** LLM agents vary run to run.

---

## Guardrails (enforced in code, not prompts)

Weaker models ignore instructions sometimes, so the important rules live in `tools.py`:

- **Run budget:** `deploy_config` refuses once the budget is used. Failed deploys count.
- **Search space:** configs outside `target.toml [search_space]` are rejected.
- **No mislabeled data:** `run_load_sweep` only measures the config that's actually deployed.
- **Workspace jail:** file tools can't read or write outside the run folder.
- **Turn caps:** every role has `max_turns` in `roles.toml`.
- **Least privilege:** each role only gets the tools listed for it. The executor can't run arbitrary code, and the analyst can't deploy.

⚠️ `run_python` (observer and analyst) executes model-written code on this
machine, inside the run folder. Run this project in a container or VM, not on
a machine with credentials you care about.

---

## Measuring with MLPerf Endpoints (optional)

[MLPerf Endpoints](https://github.com/mlcommons/endpoints) is MLCommons' benchmark for
serving endpoints, built with 35+ industry partners including Nutanix. It's the industry's
"certified dyno": real datasets, a multi-process client built for 50k+ QPS, and MLPerf's
early-stopping p99 estimates.

**What changes:** only *how each config is measured*. The agents still decide what to test
and when to stop. The adapter (`perfagents/sweep.py`) converts Endpoints' output into the
same `summary.json` and `requests.csv` files, so prompts, guardrails and evals all work
unchanged.

```bash
make mlperf-setup     # installs inference-endpoint into ./.venv-mlperf (Python 3.12+), pinned commit
make mlperf-check     # one short sweep on the mock, to prove it works on this machine

# use it for one run, or for evals (the oracle is then built with it too):
python3 -m perfagents run  --loadgen mlperf-endpoints
python3 -m perfagents eval --loadgen mlperf-endpoints
# or make it the default: target.toml [sweep] loadgen = "mlperf-endpoints"
```

Settings live in `config/target.toml` under `[sweep.mlperf_endpoints]`:

| Setting | Default | Notes |
|---|---|---|
| `dataset` | `bench/datasets/mixed_prompts.jsonl` | JSONL with a `prompt` field. Swap in a sample of your real traffic |
| `tokenizer` | the target model's name | a Hugging Face repo id or local path. Endpoints counts tokens client-side (TPS, TPOT); for the mock it uses `bench/mock_tokenizer` |
| `api_type` | `openai` | `/v1/chat/completions`. Also `openai-completions` or `sglang` |
| `workers` | `-1` (auto) | client processes. On machines with ≤4 CPUs the adapter uses 1 and skips CPU pinning, because auto can't start there |
| `extra_args` | `[]` | any other `inference-endpoint` flag |

**What you get per level**, in addition to the usual fields:
- `ttft_p99_early_stopping_ms`: MLPerf's conservative p99 TTFT. It's `null` when a level has
  too few requests for a confident estimate, so use longer `duration_s` for it to appear.
  The SLO check still uses `ttft_p99_ms`, so results stay comparable with the builtin backend.
- `interactivity_tokens_per_s_per_user`: the per-user token rate on MLPerf's Pareto charts.
- The raw MLPerf report of every level, kept under `results/<config>/mlperf_endpoints/c<N>/`.

**Good to know:**
- **Startup cost:** each concurrency level starts a fresh `inference-endpoint` process, about
  30-40 s of fixed startup on a small VM. Use fewer, longer levels with this backend.
- **Size the client host:** the client needs real CPU. On a 2-vCPU VM, high concurrency
  starves the client and inflates TPOT; run it on a proper load-generator host near the cluster.
- **ITL vs TPOT:** `itl_*` fields hold MLPerf's TPOT (time per output token), and
  `requests.csv` has no per-request token counts with this backend.
- **Not included yet:** the accuracy gate (GPQA / AIME pass@1). Endpoints supports it with
  `--dataset acc:...`, but the datasets come from Hugging Face; it's the natural next step.

---

## Testing a real NAI endpoint instead of the mock

In `config/target.toml`, set `type = "command"` and fill in `[command]`:

- `url`, `model`, `api_key_env`: the endpoint under test (both load generators speak OpenAI streaming).
- `deploy_command`: a shell command that redeploys with `{tp}` and `{max_batch}`
  (for example a script that patches the vLLM Deployment's args, or calls NAI's API).
- `ready_url`: polled until it returns 200 after each deploy.

Note: `make eval` on a real target builds the oracle by deploying **every**
config once. That costs real GPU time, so keep the search space small.

---

## Design notes (why it's built this way)

- **OpenAI-compatible API, not Anthropic's:** every vLLM, NAI and NIM endpoint
  supports it with tool calling, so each role can use a different model on a
  different endpoint.
- **Own agent loop (`agent.py`) instead of a framework:** about 60 lines, easy
  to read and debug, and it records the exact metrics the evals need.
- **Specialists start fresh on every task:** this keeps their context short,
  which matters for smaller models. The planner must therefore put everything
  needed into each task, and its prompt says so.
- **Mechanical tools for the executor:** it gets `deploy_config` and
  `run_load_sweep` instead of a shell. This means less room for a weak model to
  go wrong, and guardrails are easy to enforce.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `check-models` → `DOWN ... needs env var` | export the `api_key_env` from models.toml |
| `check-models` → `NO-TOOL` | vLLM missing `--enable-auto-tool-choice` or wrong `--tool-call-parser` |
| `HTTP 400 ... model does not exist` | `model` in models.toml must equal vLLM's `--served-model-name` |
| many `tool_errors` for one model | its tool-call parser is wrong, or the model is weak at tool use; check the transcript |
| `max_turn_hits` > 0 | the agent looped; read its part of `transcript.jsonl`, then tune its prompt or `max_turns` |
| quality 0 with note "never tested" | the planner recommended a config it didn't measure (a reasoning failure) |
| `MLCommons inference-endpoint is not installed` | run `make mlperf-setup` (needs Python 3.12+) |
| `Workers failed to initialize` | the client host is too small; set `[sweep.mlperf_endpoints] workers = 1` |
| MLPerf TPOT much higher than expected | the client is CPU-starved; lower concurrency or use a bigger load-generator host |
