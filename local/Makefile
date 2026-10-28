# Shortcuts. Run `make help` to list them. Everything is plain Python; no installs needed,
# except the optional MLPerf Endpoints load generator (make mlperf-setup).
PY ?= python3
# MLCommons MLPerf Endpoints, pinned to a tested commit. Bump deliberately, then run `make mlperf-check`.
MLPERF_REF ?= 4b385eb53cac362e47d3fb14a78a566ebf181e7d

.PHONY: help check run eval eval-judge oracle test smoke clean mlperf-setup mlperf-check smoke-mlperf

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-14s %s\n", $$1, $$2}'

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

mlperf-setup:    ## install MLCommons MLPerf Endpoints into ./.venv-mlperf (needs Python 3.12+)
	$(PY) -c 'import sys; assert sys.version_info >= (3, 12), "MLPerf Endpoints needs Python 3.12+"'
	$(PY) -m venv .venv-mlperf
	.venv-mlperf/bin/pip install -q --upgrade pip
	.venv-mlperf/bin/pip install -q "inference-endpoint @ git+https://github.com/mlcommons/endpoints@$(MLPERF_REF)"
	@echo "Installed. Verify with: make mlperf-check"

mlperf-check:    ## one short MLPerf Endpoints sweep on the mock, to verify the install
	$(PY) -m perfagents check-loadgen --loadgen mlperf-endpoints

smoke-mlperf:    ## fake-LLM agents measuring with MLPerf Endpoints (~5 min on a small VM)
	@$(PY) tests/fake_llm.py --port 8599 & echo $$! > .fake_llm.pid; sleep 1; \
	$(PY) -m perfagents run --all-models fake --budget 3 --loadgen mlperf-endpoints \
	    --levels 4,16 --duration 3; \
	status=$$?; kill `cat .fake_llm.pid`; rm -f .fake_llm.pid; exit $$status

clean:           ## delete run folders, eval results and cached oracles
	rm -rf runs evals/results evals/oracles
