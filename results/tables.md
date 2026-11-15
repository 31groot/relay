## Table 2a: Latency
| Scenario | Real or stand-in | Requests sent | Duration (s) | Offered rate (per min) | First token p50 / p95 / p99 (ms) | Total time p50 / p95 / p99 (ms) |
|---|---|---|---|---|---|---|
| steady16 | real | not measured | not measured | not measured | not measured | not measured |
| steady16 | stand-in | 16 | 502 | 1.9 | 1603 / 4705 / 4705 (n=16) | 1603 / 4705 / 4705 (n=16) |
| steady | stand-in | 63 | 1728 | 2.2 | 1804 / 5303 / 16679 (n=63) | 1804 / 5303 / 16679 (n=63) |
| burst | stand-in | 63 | 1192 | 3.2 | 2600 / 15551 / 19474 (n=63) | 2600 / 15551 / 19474 (n=63) |
| overload | stand-in | 133 | 863 | 9.2 | 6283 / 18031 / 19369 (n=132) | 6393 / 18233 / 19920 (n=133) |
| tpm_halved | stand-in | 63 | 1728 | 2.2 | 1622 / 16921 / 19901 (n=63) | 1622 / 16921 / 19901 (n=63) |
| daily_exhausted | stand-in | 37 | 864 | 2.6 | 149 / 1574 / 1814 (n=37) | 149 / 1574 / 1814 (n=37) |
| burst_naive | stand-in | 63 | 1192 | 3.2 | 1760 / 6857 / 15003 (n=34) | 3305 / 20161 / 20189 (n=63) |
| overload_naive | stand-in | 133 | 863 | 9.2 | 4596 / 19009 / 19952 (n=59) | 20016 / 20082 / 20110 (n=133) |
| probe | real | 32 | 1213 | 1.6 | 379 / 689 / 969 (n=32) | 386 / 698 / 969 (n=32) |
| smoke | real | 7 | 346 | 1.2 | 1340 / 21969 / 21969 (n=5) | 664 / 21969 / 21969 (n=6) |
| quality_default | real | 17 | 159 | 6.4 | 15070 / 41064 / 41064 (n=12) | 15070 / 41064 / 41064 (n=12) |
| quality_baseline | real | 17 | 156 | 6.6 | 15104 / 26264 / 26264 (n=12) | 15104 / 26264 / 26264 (n=12) |
| quality_wide | real | 17 | 146 | 7.0 | 14526 / 24083 / 24083 (n=12) | 14526 / 24083 / 24083 (n=12) |

## Table 2b: Outcomes
| Scenario | Succeeded % (served / full answer) | Rate-limited % (status / without a full answer) | Timed out or given up % | Retries per request | Peak queue length | Tokens per answer | Cost per 1,000 answers (served / full answer) |
|---|---|---|---|---|---|---|---|
| steady16 | not measured | not measured | not measured | not measured | not measured | not measured | not measured |
| steady16 | 100.0 / 100.0 | 0.0 / 0.0 | 0.0 | 0.00 | not measured | 3119 | $0.38 / $0.38 |
| steady | 100.0 / 100.0 | 0.0 / 0.0 | 0.0 | 0.00 | 0 | 3288 | $0.41 / $0.41 |
| burst | 100.0 / 84.1 | 44.4 / 15.9 | 0.0 | 0.00 | 15 | 3387 | $0.20 / $0.23 |
| overload | 99.2 / 73.7 | 51.9 / 25.6 | 0.8 | 0.00 | 11 | 3177 | $0.17 / $0.22 |
| tpm_halved | 100.0 / 88.9 | 31.7 / 11.1 | 0.0 | 0.00 | 1 | 3341 | $0.26 / $0.29 |
| daily_exhausted | 100.0 / 59.5 | 67.6 / 40.5 | 0.0 | 0.00 | 0 | 3152 | $0.04 / $0.06 |
| burst_naive | 54.0 / 54.0 | 0.0 / 0.0 | 46.0 | 2.00 | 0 | 3358 | $0.36 / $0.36 |
| overload_naive | 44.4 / 44.4 | 0.0 / 0.0 | 55.6 | 12.92 | 0 | 3228 | $0.36 / $0.36 |
| probe | 100.0 / 100.0 | 0.0 / 0.0 | 0.0 | 0.00 | not measured | 309 | $0.13 / $0.13 |
| smoke | 85.7 / 85.7 | 14.3 / 14.3 | 0.0 | 0.00 | not measured | 3223 | $0.42 / $0.42 |
| quality_default | 100.0 / 100.0 | 0.0 / 0.0 | 0.0 | 0.00 | not measured | 2939 | $0.32 / $0.32 |
| quality_baseline | 100.0 / 100.0 | 0.0 / 0.0 | 0.0 | 0.00 | not measured | 2782 | $0.30 / $0.30 |
| quality_wide | 100.0 / 100.0 | 0.0 / 0.0 | 0.0 | 0.00 | not measured | 2979 | $0.31 / $0.31 |

## Retry comparison (stand-in, same seeded arrivals and question sequence)
| Load (same arrivals) | Client | Requests | Served % / full answer % | Given up % | Retries (total / per request) | Rate-limited % | Total time p50 / p95 (ms) | Tokens spent | Wasted tokens (measured) | Calls cancelled mid-flight |
|---|---|---|---|---|---|---|---|---|---|---|
| burst | limiter + backoff + retry budget | 63 | 100.0 / 84.1 | 0.0 | 0 / 0.00 | 44.4 | 2600 / 15551 | 84673 | 0 | 0 |
| burst | naive: retry every 429 immediately | 63 | 54.0 / 54.0 | 46.0 | 126 / 2.00 | 0.0 | 3305 / 20161 | 83952 | 2570 | 29 |
| overload | limiter + backoff + retry budget | 133 | 99.2 / 73.7 | 0.8 | 0 / 0.00 | 51.9 | 6393 / 18233 | 146154 | 0 | 1 |
| overload | naive: retry every 429 immediately | 133 | 44.4 / 44.4 | 55.6 | 1719 / 12.92 | 0.0 | 20016 / 20082 | 142039 | 5101 | 74 |

- burst: naive retrying made it WORSE — users who gave up 29/63 vs 0/63, served 54.0% vs 100.0%
- overload: naive retrying made it WORSE — users who gave up 74/133 vs 1/133, served 44.4% vs 99.2%

## Table 3: Retrieval settings (real, 17 answer questions)
| Setting (piece size, pieces fetched, re-rank yes or no) | Questions asked | Correct and cited % | Wrong % | Correctly refused % | Tokens per answer | First token p50 / p95 (ms) | Total time p50 / p95 (ms) |
|---|---|---|---|---|---|---|---|
| 200 tok, 8 pieces, hybrid RRF, no rerank | 17 | 35.3 | 5.9 | 58.8 | 2939 | 15070 / 41064 (n=12) | 15070 / 41064 (n=12) |
| 200 tok, 8 pieces, BM25, no rerank | 17 | 35.3 | 5.9 | 58.8 | 2782 | 15104 / 26264 (n=12) | 15104 / 26264 (n=12) |
| 800 tok, 2 pieces, hybrid RRF, no rerank | 17 | 29.4 | 5.9 | 58.8 | 2979 | 14526 / 24083 (n=12) | 14526 / 24083 (n=12) |

Definitions: full answer = the pipeline's own outcome reached the user (judged answer, cached copy of one, or a gate/NOT_IN_PAPERS refusal); served = full answer or the zero-token extractive fallback. Wasted tokens = measured usage on requests that ended without their own outcome; calls cancelled mid-flight are counted, not estimated.

Notes: stand-in times are simulated (time compressed ×20, converted back). Real quality-run times include TPM pacing waits (runs were back-to-back); gate refusals make no model call and are not timed. Refused-wrongly % = 100 − correct − wrong − correctly refused. Percentiles are nearest-rank; with n < 100, p99 is the slowest request.
