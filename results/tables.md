## Table 2a: Latency
| Scenario | Real or stand-in | Requests sent | Duration (s) | Offered rate (per min) | First token p50 / p95 / p99 (ms) | Total time p50 / p95 / p99 (ms) |
|---|---|---|---|---|---|---|
| steady | real | not measured | not measured | not measured | not measured | not measured |
| steady | stand-in | 63 | 1728 | 2.2 | 1728 / 5218 / 16497 (n=63) | 1728 / 5218 / 16497 (n=63) |
| burst | stand-in | 63 | 1192 | 3.2 | 2513 / 15510 / 19321 (n=63) | 2513 / 15510 / 19321 (n=63) |
| overload | stand-in | 133 | 863 | 9.2 | 6371 / 18178 / 19286 (n=132) | 6382 / 18186 / 19766 (n=133) |
| tpm_halved | stand-in | 63 | 1728 | 2.2 | 1546 / 16968 / 19889 (n=63) | 1546 / 16968 / 19889 (n=63) |
| daily_exhausted | stand-in | 37 | 864 | 2.6 | 146 / 1547 / 1795 (n=37) | 146 / 1547 / 1795 (n=37) |
| probe | real | 32 | 1213 | 1.6 | 379 / 689 / 969 (n=32) | 386 / 698 / 969 (n=32) |
| smoke | real | 7 | 346 | 1.2 | 1340 / 21969 / 21969 (n=5) | 664 / 21969 / 21969 (n=6) |
| quality_default | real | 17 | 159 | 6.4 | 15070 / 41064 / 41064 (n=12) | 15070 / 41064 / 41064 (n=12) |
| quality_baseline | real | 17 | 156 | 6.6 | 15104 / 26264 / 26264 (n=12) | 15104 / 26264 / 26264 (n=12) |
| quality_wide | real | 17 | 146 | 7.0 | 14526 / 24083 / 24083 (n=12) | 14526 / 24083 / 24083 (n=12) |

## Table 2b: Outcomes
| Scenario | Succeeded % | Rate-limited % | Timed out or given up % | Retries per request | Peak queue length | Tokens per answer | Cost per 1,000 answers |
|---|---|---|---|---|---|---|---|
| steady | not measured | not measured | not measured | not measured | not measured | not measured | not measured |
| steady | 100.0 | 0.0 | 0.0 | 0.00 | 0 | 3288 | $0.41 |
| burst | 55.6 | 44.4 | 0.0 | 0.00 | 15 | 3387 | $0.36 |
| overload | 47.4 | 51.9 | 0.8 | 0.00 | 11 | 3158 | $0.35 |
| tpm_halved | 68.3 | 31.7 | 0.0 | 0.00 | 1 | 3341 | $0.38 |
| daily_exhausted | 32.4 | 67.6 | 0.0 | 0.00 | 0 | 3152 | $0.12 |
| probe | 100.0 | 0.0 | 0.0 | 0.00 | not measured | 309 | $0.13 |
| smoke | 85.7 | 14.3 | 0.0 | 0.00 | not measured | 3223 | $0.42 |
| quality_default | 100.0 | 0.0 | 0.0 | 0.00 | not measured | 2939 | $0.32 |
| quality_baseline | 100.0 | 0.0 | 0.0 | 0.00 | not measured | 2782 | $0.30 |
| quality_wide | 100.0 | 0.0 | 0.0 | 0.00 | not measured | 2979 | $0.31 |

## Table 3: Retrieval settings (real, 17 answer questions)
| Setting (piece size, pieces fetched, re-rank yes or no) | Questions asked | Correct and cited % | Wrong % | Correctly refused % | Tokens per answer | First token p50 / p95 (ms) | Total time p50 / p95 (ms) |
|---|---|---|---|---|---|---|---|
| 200 tok, 8 pieces, hybrid RRF, no rerank | 17 | 35.3 | 5.9 | 58.8 | 2939 | 15070 / 41064 (n=12) | 15070 / 41064 (n=12) |
| 200 tok, 8 pieces, BM25, no rerank | 17 | 35.3 | 5.9 | 58.8 | 2782 | 15104 / 26264 (n=12) | 15104 / 26264 (n=12) |
| 800 tok, 2 pieces, hybrid RRF, no rerank | 17 | 29.4 | 5.9 | 58.8 | 2979 | 14526 / 24083 (n=12) | 14526 / 24083 (n=12) |

Notes: stand-in times are simulated (time compressed ×20, converted back). Real quality-run times include TPM pacing waits (runs were back-to-back); gate refusals make no model call and are not timed. Refused-wrongly % = 100 − correct − wrong − correctly refused. Percentiles are nearest-rank; with n < 100, p99 is the slowest request.
