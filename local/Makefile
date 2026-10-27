# Shortcuts. Run `make help` to list them. Everything is plain Python; no installs needed.
PY ?= python3

.PHONY: help check run eval eval-judge oracle test smoke clean

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-12s %s\n", $$1, $$2}'

check:           ## ping the model endpoints used in roles.toml, verify tool calling
	$(PY) -m perfagents check-models

run:             ## one perf test with the default models (see config/roles.toml)
	$(PY) -m perfagents run

eval:            ## compare model line-ups from evals/suite.toml
	$(PY) -m perfagents eval

eval-judge:      ## same, plus the judge model grades every report.md
	$(PY) -m perfagents eval --judge

oracle:          ## (re)build the brute-force answer key used by evals
	$(PY) -m perfagents oracle --rebuild

test:            ## unit tests for scoring and guardrails (no LLM needed)
	$(PY) -m unittest discover -s tests -t . -v

smoke:           ## full pipeline offline with the scripted fake LLM (~2 min)
	@$(PY) tests/fake_llm.py --port 8599 & echo $$! > .fake_llm.pid; sleep 1; \
	$(PY) -m perfagents check-models --models fake && \
	$(PY) -m perfagents eval --suite tests/smoke_suite.toml --judge-model fake; \
	status=$$?; kill `cat .fake_llm.pid`; rm -f .fake_llm.pid; exit $$status

clean:           ## delete run folders, eval results and cached oracles
	rm -rf runs evals/results evals/oracles
