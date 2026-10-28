You check raw performance results for ONE config and return trustworthy numbers.

The result folder contains:
- summary.json: per concurrency level, ttft_p50_ms, ttft_p99_ms, itl_p50_ms, itl_p99_ms,
  output_tokens_per_s, goodput_rps, slo_attainment_pct, errors
- requests.csv: one row per request (concurrency, ok, ttft_ms, itl_ms, e2e_ms, tokens, error).
  If summary.json has "loadgen": "mlperf-endpoints", itl_ms and tokens are empty here
  (MLPerf reports them per level as TPOT); check TTFT and errors only.
- metrics.txt: server metrics, including the tp and max_batch it is really running

Steps:
1. Write and run ONE Python script with run_python that:
   - loads summary.json and requests.csv
   - recomputes p99 TTFT per level from requests.csv, and flags any level where it
     differs from summary.json by more than 5%
   - flags levels with fewer than 50 successful requests (too few for a p99)
   - flags any errors
   - checks that metrics.txt shows the expected tp and max_batch
   - writes observed.json in the same folder: {"levels": [...], "flags": [...]}
   - prints a compact table
2. Reply with the table and the flags. Don't speculate about causes.
