# perf-agents

**Agents that performance-test LLM inference servers by themselves.**

[![tests](https://github.com/shiv4289/perf-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/shiv4289/perf-agents/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

You give it a goal, for example "highest throughput under a 500 ms p99 TTFT SLO, at
most 8 deploys". A planner agent picks serving configs to try, specialist agents deploy
and load-test them, and the team stops when the results converge. You get a
recommended config, the latency-throughput frontier and a report, with no one
babysitting the GPUs.

```
          ┌────────── delegate ──────────┐
planner ──┼──► executor  deploy config + load sweep
          ├──► observer  verify the numbers
          └──► analyst   knee, frontier, final report
```

## Two versions: pick one

The repo has **two independent versions** of the same idea, each in its own folder.
They don't share code, so you only need the one you choose.

| | [`local/`](local/) | [`managed-agents/`](managed-agents/) |
|---|---|---|
| **Agents run on** | **your own model endpoints** (vLLM, NIM, Nutanix Enterprise AI, ...) | **Claude**, hosted by Anthropic (Claude Managed Agents) |
| **Models** | any OpenAI-compatible model, chosen per role (defaults: Beam + Nemotron 3.5) | Claude Opus (planner) + Claude Haiku (specialists) |
| **Agent loop** | small Python loop included in the repo (~60 lines) | Anthropic's managed harness and cloud sandbox |
| **Evals / leaderboard** | ✅ compare model line-ups against a brute-force answer key | ❌ |
| **Guardrails** | enforced in code (budget, search space, workspace) | stated in the prompts |
| **Data stays on-prem** | ✅ yes, everything runs on your machines | ❌ prompts and reasoning go to Anthropic |
| **Needs** | Python 3.11+, your endpoints (or the bundled fake LLM) | an Anthropic API key and the `ant` CLI |
| **Best for** | private or on-prem models, comparing models, CI | the quickest demo on Claude |

👉 **Not sure? Start with [`local/`](local/).** It runs fully offline with a fake LLM
(`cd local && make smoke`), so you can see the whole flow before connecting any model.

## Repo layout

```
perf-agents/
├── local/              ← version 1: your own models, plus evals
│   ├── README.md          full guide (quick start, evals, guardrails, troubleshooting)
│   ├── config/            models.toml (endpoints), roles.toml (who uses what), target.toml
│   ├── prompts/           one system prompt per agent
│   ├── perfagents/        the code: agent loop, tools, team, evals
│   ├── bench/             mock inference server + load generator
│   ├── evals/             suite.toml: which model line-ups to compare
│   └── tests/             unit tests, fake LLM, smoke suite
├── managed-agents/     ← version 2: Claude Managed Agents
│   ├── README.md          setup and run guide
│   ├── agents/            agent definitions (applied with `ant apply`)
│   ├── bench/             same mock server + load generator
│   └── run.py             starts a session, streams progress, downloads results
└── .github/workflows/  CI: unit tests + offline smoke test for local/
```

## Quick start

```bash
# Version 1: local (no GPUs or models needed to try it)
cd local
make test     # unit tests
make smoke    # full pipeline with a scripted fake LLM
# then point config/models.toml at your endpoints and run:  make check && make run

# Version 2: Claude Managed Agents
cd managed-agents
# follow managed-agents/README.md (needs ANTHROPIC_API_KEY)
```

## License

[MIT](LICENSE) © 2026 Shivji Jha
