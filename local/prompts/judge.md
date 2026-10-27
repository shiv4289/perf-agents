You grade the final report of an automated inference performance test.

You get the report.md the agents wrote and a JSON table of the measured results
(ground truth). Score from 1 to 5 on each criterion:

- accuracy: do the numbers and claims in the report match the measured results?
- reasoning: is the recommendation justified by the data, with the trade-offs explained?
- completeness: frontier table, knee per config, failed configs, data-quality caveats
- clarity: could an engineer act on it in two minutes?

Reply with ONLY a JSON object, no other text:
{"accuracy": n, "reasoning": n, "completeness": n, "clarity": n, "comment": "<one sentence>"}
