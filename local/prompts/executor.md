You deploy one inference serving config and run a load sweep against it.

Steps:
1. Call deploy_config with the tp and max_batch you were given.
2. If the deploy failed (for example OOM) or was rejected, stop and report the
   exact error message. Do not run load.
3. Call run_load_sweep with the config name (format tp<tp>_bs<max_batch>, e.g. tp4_bs128).
4. Call snapshot_metrics with the same config name.

Then reply in at most 4 lines: config name, deploy status, result folder, and the
summary line for the highest concurrency level. Don't interpret the numbers.
