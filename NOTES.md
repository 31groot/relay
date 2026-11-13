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
