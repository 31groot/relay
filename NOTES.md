# Relay — working notes

## 2026-11-14 — Phase 0.1: verified papers

Verified from the arXiv stamp on page 1 (`pdftotext`), page count (`pdfinfo`), and HEAD request
(HTTP 200, `application/pdf`, no redirect, Content-Length = local size). arXiv publishes no checksums,
and it regenerates PDFs (v7 dated 2 Aug 2023, PDF last-modified Apr 2024), so these hashes pin the bytes we fetched.

| Paper | Expected | Verified stamp | Pages | SHA-256 | Source |
|---|---|---|---|---|---|
| Attention Is All You Need | 1706.03762v7 | `arXiv:1706.03762v7 [cs.CL] 2 Aug 2023` | 15 | `bdfaa68d8984f0dc02beaca527b76f207d99b666d31d1da728ee0728182df697` | https://arxiv.org/pdf/1706.03762v7 |
| Moshi: a speech-text foundation model for real-time dialogue | 2410.00037v1 | `arXiv:2410.00037v1 [eess.AS] 17 Sep 2024` | 66 | `a687144cc77bea86b0c60ae800ccb1d3e0816033fa5c7960388fe29a0dbfcfac` | https://arxiv.org/pdf/2410.00037v1 |
| Dynamic Chunking for End-to-End Hierarchical Sequence Modeling | 2507.07955v2 | `arXiv:2507.07955v2 [cs.LG] 15 Jul 2025` | 39 | `04a2b32e6e82853cfc23c5db132750874a86cde99219ec6f664128f4aa83122f` | https://arxiv.org/pdf/2507.07955v2 |

Phase 2 notes: Moshi is 66/120 pages (55%) → retrieval-dominance risk. Page-1 noise to strip:
arXiv stamp, Moshi duplicated running title, Google permission notice (Attention).

## 2026-11-14 — Groq free-plan limits (from console.groq.com/settings/limits, checked by user)

| Model | RPM | RPD | TPM | TPD |
|---|---|---|---|---|
| openai/gpt-oss-120b | 30 | 1K | 8K | 200K |
| openai/gpt-oss-20b | 30 | 1K | 8K | 200K |
| qwen/qwen3.8-27b (preview) | 30 | 1K | 8K | 200K |
| allam-2-7b | 30 | 7K | 6K | 500K |

- `llama-3.1-8b-instant`, `llama-3.3-70b-versatile`: not in the console → unavailable.
- Docs (https://console.groq.com/docs/rate-limits, fetched 2026-11-14): limits apply per organization;
  `x-ratelimit-*-requests` = RPD, `x-ratelimit-*-tokens` = TPM; no TPD header; `retry-after` (s) only on 429;
  cached tokens don't count. 429 body undocumented.
- Binding limits are TPM and TPD, not RPD: ~2.5K tokens/generation → ~3 generations/min, ~80/day per model.

## 2026-11-14 — Quota split (per model, per day)

| Bucket | Share | Tokens/day (200K-TPD models) |
|---|---|---|
| Development | 10% | 20K |
| Smoke test | 5% | 10K |
| Answer evaluation | 50% | 100K |
| Real load run | 25% | 50K |
| Spare | 10% | 20K |

The one-time Phase 1 probe is paid from the spare 10% and capped at **20K tokens total** (all models combined).

## Open questions the probe must answer

1. Do models have separate quota pools, or is quota shared per organization?
2. Does `max_tokens` count toward TPM?
3. How many reasoning tokens do gpt-oss models use at the lowest reasoning effort?
4. What are the exact 429 body and headers?

## 2026-11-14 — Phase 1.1 probe findings (results/probe_log.jsonl, 30 calls, all 200, 9,690 tokens)

Setup: sequential, 8 s pause, round-robin across models, `max_completion_tokens=300`, temperature 0,
`reasoning_effort` low (gpt-oss) / none (qwen). Per model: 8 short, 1 medium (~690 tok), 1 large (~1,630 tok).
n=10 per model → medians and max only; no percentiles claimed.

| Model | Total ms median / max | Groq queue_time median | Server total_time median | Template overhead (short prompt tokens) |
|---|---|---|---|---|
| gpt-oss-120b | 376 / 663 | 159 ms | 102 ms | ~60 (80–84 tok for a ~20-tok question) |
| gpt-oss-20b | 528 / 698 | 341 ms | 62 ms | ~60 (80–84) |
| qwen3.8-27b | 252 / 673 | 50 ms | 65 ms | ~2 (21–25) |

First chunk / first content (ms): short = median (max) over 8; medium, large = single call.

| Model | Short | Medium | Large | Reasoning tokens S / M / L |
|---|---|---|---|---|
| gpt-oss-120b | 337 / 337 (628) | 306 / 307 | 568 / 608 | 5–8 / 6 / 15 |
| gpt-oss-20b | 485 / 490 (689) | 523 / 524 | 506 / 506 | 5–6 / 23 / 15 |
| qwen3.8-27b | 197 / 197 (635) | 241 / 241 | 384 / 384 | none (field absent) |

- Q3 (reasoning at lowest effort): 5–23 tokens; delays first content by 0–40 ms. Negligible.
- Latency is dominated by Groq-side queueing, not generation (20b: queue 341 ms vs 62 ms work). Hypothesis from one session.
- Short answers: first token ≈ total (within 20–150 ms).
- Q1 (separate pools): **RPD separate per model — high confidence** (each model's remaining-requests fell only by its
  own calls: 999, 999, 998 … 991). **TPM separate per model — medium confidence** (large-prompt round: 20b's header
  showed only its own 1,687 tokens 8.5 s after 120b's 1,685-token call; a shared bucket would show ~2,200).
  TPM refills linearly at 8000/60 = 133.3 tok/s (`reset-tokens` = charged / 133.3).
- Q2 (max_tokens vs TPM): see "Phase 1.2" below — the 1.1 reading ("reserves prompt + max_tokens") was **wrong**.

## 2026-11-14 — Locked model pair

| Role | Model | Reason |
|---|---|---|
| Generator | openai/gpt-oss-120b | Strongest production model available; reasoning overhead at `low` measured negligible |
| Judge | openai/gpt-oss-20b | Separate quota pool (Q1) so judging doesn't compete with generation; cheapest; short verdicts. Watch: highest queue time in probe |
| Fallback | qwen/qwen3.8-27b | Lowest latency and ~60 fewer tokens/call; third pool. Risk: preview status, highest price |

## 2026-11-14 — Phase 1.2: max_tokens vs TPM (calls 31–32, gpt-oss-20b, prompt "Reply with the single word OK.", 78 tok)

Bucket full before each call (≥ 228 s since previous 20b call; refill 133.3 tok/s).

| Call | max_completion_tokens | Status | Actual usage | TPM header drop | prompt + max |
|---|---|---|---|---|---|
| 1.1 (30 calls) | 300 | 200 | 46–1,711 | = prompt+max in 10/30, ≈ usage in 20/30 | — |
| 31 | 8,000 | **200** | 95 | **835** | 8,078 |
| 32 | 2,000 | 200 | 95 | **95** | 2,078 |

**Q2 result:**
- `max_completion_tokens` is **not** an admission check against TPM: a request whose prompt + max (8,078) exceeds the
  entire 8K TPM limit was accepted. **High confidence** (direct observation).
- The drop does **not** scale with max_tokens (2,000 → 95 = actual usage; 8,000 → 835, unexplained). The 1.1 "prompt + 300"
  drops are unexplained by a single rule; header snapshots are inconsistent. How Groq charges in-flight requests:
  **unknown, low confidence** in any model of it. Daily/settled charge = actual usage: **medium-high confidence**.

**Limiter rule (corrects the 1.1 note):** reserve an **estimate** (prompt tokens + expected output for that step),
not max_tokens; re-sync from `x-ratelimit-remaining-tokens` on every response; treat the 429 (Phase 7) as the ground truth.
Reserving full max_tokens would be more conservative than Groq and waste burst capacity.
Keep max_tokens tight per step anyway — it bounds runaway output cost and latency, not admission.

**Q4 (exact 429 body/headers, recovery time):** not captured in Phase 1 (no deliberate burst, option (b) declined).
**Capture during Phase 7** real load run; the app logs full error bodies and all headers for every non-200.

**Probe closed:** 32 calls, 9,880 tokens charged (cap 20,000).
