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
4. What are the exact 429 body and headers? — **ANSWERED 2026-11-14 (Phase 4 run 1), see below.**

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

## 2026-11-14 — Phase 3 retrieval hit rule (user decision)

A retrieved chunk is a **hit** for a retrieval question only if all three hold:
1. the chunk is from the question's stated paper;
2. `page_start <= stated page <= page_end`;
3. the chunk contains the key phrase, matched **whitespace-insensitively after NFKC** (same matcher as
   `exp/validate_questions.py`). Any `alt_phrases` entry also counts as a match.

Why: short numeric key phrases recur elsewhere (R16 `1024` on 9 library pages, R36 `7.0` on 6), so a
phrase-only rule would over-count hits. Questions with `source_pages`: stated `page` is the gold page.

## 2026-11-14 — Embedding model files (Phase 2.3b)

BAAI/bge-small-en-v1.5 at pinned revision `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`, saved to
`data/models/bge-small-en-v1.5/` (gitignored). Config: 384 dims, 512-token window, CLS pooling + normalize.

| File | Source path | Bytes | SHA-256 | Independent check |
|---|---|---|---|---|
| model.onnx | `onnx/model.onnx` | 133,093,490 | `828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35` | = HF lfs.oid |
| tokenizer.json | `tokenizer.json` | 711,396 | `d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66` | git blob = HF oid `688882a7…` |

Runtime: onnxruntime 1.32.0, tokenizers 0.23.3, numpy 2.5.4 (pinned in requirements.txt). Installing them also
pulled transitive deps (huggingface-hub, hf-xet, fsspec, protobuf, …) — not used directly.

## 2026-11-14 — Phase 3 checkpoint decisions (FINAL, user decision)

Source: `results/retrieval_eval.csv`, `results/gate_scores.csv`, `results/retrieval_report.txt` (commit ce86bec).
40 retrieval questions; budget = top-8 @200, top-4 @400, top-2 @800 (~1,600 context tokens each).

| Role | Setting | R@budget | R@10 |
|---|---|---|---|
| **Default** | hybrid (RRF k=60), 200-token chunks, top-8 | 34/40 (85.0%) | 35/40 (87.5%) |
| **Baseline** | BM25, 200, top-8 | 34/40 (85.0%) | 36/40 (90.0%) |
| **Wide context** | hybrid, 800, top-2 | 31/40 (77.5%) | 38/40 (95.0%) |
| Dropped | semantic-only (best 28/40), hybrid 400 (30/40), BM25-only gating | | |

Budget recall, all settings: bm25 34 / 34 / 30; semantic 28 / 27 / 25; hybrid 34 / 30 / 31 (sizes 200 / 400 / 800).

**Gate:** answer only if semantic top-1 cosine ≥ **0.60** at size 200. INDICATIVE: based on 5 off-topic
questions. Highest off-topic 0.551, lowest answerable 0.649 → 0.60 sits ~0.049 from each; one new off-topic
question scoring > 0.60 or answerable < 0.60 breaks it. Off-topic false-answer 0/5, false-refusal 0/47 at this set.
Near-miss questions are NOT expected to be caught by the gate (4–5/5 pass at every size); `NOT_IN_PAPERS` + the
judge handle them, and Phase 5 reports them separately. BM25 top score cannot gate (off-topic outscores the weakest
answerable at every size; margins −2.1 / −4.6 / −6.1).

**800 averaging confound:** best-piece is no better than averaged (semantic 800 R@budget 22 vs 25; hybrid 29 vs 31;
R@5/R@10 identical). BM25 (unaffected by averaging) also drops at 800 (34 → 30). Conclusion: the 2-chunk budget,
not embedding averaging, limits the 800 setting.

**Span-only hits:** 0 within budget for every retriever and size (all chunks: 0 / 2 / 1 span-only matches at
200 / 400 / 800). The hit rule does not favour large chunks here.

**Router (explicit paper names only):** routes 12/40, all to the gold paper, and changes one cell only
(bm25 800 R@10 37 → 38). Kept as specified; little measured benefit on this set.

## 2026-11-14 — Retrieval failure modes (write-up material; nothing tuned against the 40 questions)

- **No stemming (known limitation):** R14 asks "parameters", text has "7B-parameter" (BM25 rank 79);
  R27 asks "GPU", text has "H100 GPUs" (BM25 rank 49).
- **Moshi transcript appendix pulls BM25:** p63–64 are full of "USER: … MOSHI: …", so they outrank the abstract
  for questions containing "Moshi" (R13 top-3 = p63, p27–28, p63–64; R30 has p64 at #2).
- **Semantic weak on table values at 200:** misses R17, R35, R36 (answer is a cell value; R17 gold rank 86).
- **"Layers" matches across papers:** R01 (Attention encoder layers) — semantic top-1 at 400 was a DC chunk,
  #3 Moshi's "8 layers"; top-3 cosines within 0.736–0.748.

Known limitation (retrieval): no stemming or plural folding in the BM25 tokenizer. Not changed (user decision:
no tuning against the evaluation questions).

## 2026-11-14 — Phase 4 run 1 (smoke test): real 429, citation drift

Log: `results/phase4_log.jsonl` (org ID redacted in place, 7 lines kept). 3 generator calls on gpt-oss-120b
(2,400 / 2,570 / 2,512 tokens), then a 429; 0 judge calls; the 4th question never ran.

**Q4 answered — the real 429 (tokens per minute):**
- Status **429**, rejected in **~75 ms** (fails fast, no tokens charged).
- Body: `{"error": {"message": "Rate limit reached for model `openai/gpt-oss-120b` in organization `org_<redacted>`
  service tier `on_demand` on tokens per minute (TPM): Limit 8000, Used 7203, Requested 2522. Please try again in
  12.9375s. …", "type": "tokens", "code": "rate_limit_exceeded"}`. The body names the organization → logs redact `org_…`.
- Headers: `retry-after: 13` (s), `x-ratelimit-remaining-tokens: 797`, `x-ratelimit-reset-tokens: 54.022s`
  (time to a full bucket), `x-ratelimit-remaining-requests: 997`.
- "Used 7203" ≈ the 7,482 just spent minus ~2 s of refill → supports "charged at actual usage".
- Cause (my harness, not the prompts): no pacing between calls; 3 back-to-back generations of ~2.5K exceed 8K TPM.
  Capacity consequence: **~2.5K tokens per request on 120b → about 3 requests per minute on the free tier.**

**Q2 reopened — hypothesis (LOW confidence, n=2):** admission may check used + prompt + max_completion_tokens
against the limit: "Requested 2522" ≈ P4-3's prompt (~2,170, estimated; no usage on a rejected call) + 350.
Against it: Phase 1.2's 78 + 8,000 = 8,078 request was accepted on a full bucket. Not resolved.

**Citation drift (gpt-oss-120b):** citations came as ` [​S1]` (narrow no-break space, then a zero-width
space INSIDE the bracket) and `【S2】` (U+3010/U+3011). The deterministic check rejected correct-looking answers on
format alone, and one correction call (~2.5K tokens) was spent on format. Fix (user decision, no prompt edit):
normalize output before the check and the render — drop Unicode Cf, map Unicode spaces (Zs) to " ", 【】 → [].
Format-only failures therefore never cost a correction; real failures after normalization still do.

**Pacing (user decision):** before each call, wait until last `x-ratelimit-remaining-tokens` + refill
(limit/60 ≈ 133 tok/s) covers prompt + max_tokens + 100; reserve that at admission; re-sync from headers.
Unknown state at start-up = empty bucket. A 429 still stops the run.

**Budget for the re-run:** gpt-oss-120b used 7,482 of the 10K smoke share today; the shortfall is **borrowed from the
120b development share (20K/day): 8,000 tokens** → 120b cap 18,000 for today (7,482 used + 3 generations + one
correction ≈ 17,632 worst case). gpt-oss-20b: no borrowing, cap 10,000, 0 used.

## 2026-11-14 — Phase 4 results (paced re-run) — CHECKPOINT PASSED

Same 4 user-written questions (none from eval/questions.json), hybrid 200 top-8, gate 0.60, generator gpt-oss-120b
(max 350), judge gpt-oss-20b (max 150), citation normalization + pacing on. Log: `results/phase4_log.jsonl` (run 1 = 7
lines, run 2 = 9 lines).

| Q | Gate | Outcome | Generate tokens | Judge tokens |
|---|---|---|---|---|
| P4-1 optimizer + warmup | 0.771 | answered — Adam [S1], 4000 warmup steps [S2]; judge SUPPORTED | 2,400 | 891 |
| P4-2 base dropout | 0.715 | answered — Pdrop = 0.1 [S2]; judge SUPPORTED (stray "[Pdrop = 0.1]" bracket) | 2,517 | 615 |
| P4-3 EN→ES BLEU (near-miss) | 0.790 | refused — generator NOT_IN_PAPERS, judge skipped | 2,210 | — |
| P4-4 sourdough (off-topic) | 0.502 | refused at gate, 0 calls | — | — |

- Both answers passed the deterministic check only after normalization (format-only); no correction calls in run 2.
- Pacing: first call waited 19.4 s (empty-bucket start-up assumption), later calls 0 s; 896 TPM tokens left after P4-3.
- **Budget used today:** gpt-oss-120b **14,609** (run 1 7,482 + run 2 7,127) against cap 18,000 = smoke 10,000 +
  **4,609 of 8,000 borrowed from the 120b development share** actually used; gpt-oss-20b **1,506** of 10,000.

**Fixes after the run (user decisions):**
- Prompt-token estimator recalibrated **4.0 → 3.2 chars/token**: exact rebuild of all 8 logged calls gives a minimum of
  3.37 chars/token (P4-2 judge); 4.0 underestimated prompts by up to 12% (P4-2 generate actual 2,517 > estimated worst
  2,482). New generator worst case ≈ 3,000/call (was ≈ 2,480). Test: estimate ≥ actual prompt tokens for every logged call.
- **Stray brackets** (bracketed spans that are not citations, e.g. "[Pdrop = 0.1]") are logged per attempt as
  `stray_brackets`, never failed. Phase 5 counts them.

**Limitation — section labels:** a chunk carries the section it STARTS in. P4-1's Adam sentence is in §5.3 Optimizer
but rendered as "§5.1 Training Data and Batching" (chunk attention-200-0027 starts in §5.1 and runs into §5.3).
The page (p.7) is correct. Write-up material.

**Q2 evidence update (confidence MEDIUM-LOW):** two observations fit "admission reserves prompt + max_completion_tokens":
the 429's "Requested 2522" ≈ P4-3 prompt + 350 (run 1), and remaining-tokens after P4-1 generate (run 2) = 8,000 − 5,320
= 2,680 = prompt 2,330 + 350 exactly. Against: Phase 1.2's 78 + 8,000 = 8,078 request was accepted on a full bucket.
The pacer reserves prompt + max_tokens (+100) either way.

## 2026-11-14 — Phase 5 day 1 (default + baseline): grading rules and a judge lead

- **Grades** are in `results/grade_overrides.jsonl` (user grade final; separate from auto-grades). A06 overridden
  wrong → correct in default and baseline ("paraphrase of the same fact, cited page covers the evidence page").
- **A03 rule (user decision):** headline grade stays **wrong** when the gold page (p.3) is neither retrieved nor
  cited, even if the fact is correct; footnote column **"fact correct, cited to abstract"** (default and baseline).
  The same rule applies to the wide-context run.
- **Judge limit:** default ran with judge max_tokens 150 and only the truncated item (A01) was re-judged with 400,
  reusing the logged generation; baseline onwards ran with 400. Baseline judge completions reached 149 and 167 tokens.
- **Lead — judge consistency (n=2, not re-run):** the same 20b judge PASSED an embellishment in default A01
  ("Scaling by √d_k therefore counteracts this effect and stabilizes the gradients before the softmax is applied")
  but FLAGGED a similar one in baseline A01 attempt 1 ("This scaling therefore stabilizes the softmax computation and
  improves training of the attention mechanism" → unsupported, then corrected). Both answers are added to the
  corruption test as extra controls.

## 2026-11-14 — Phase 5 judge corruption test (gpt-oss-20b, judge max_tokens 400) — INDICATIVE, n ≤ 7 per type

Source: `results/judge_corruption.csv`, corruption rows in `results/phase5_log.jsonl` (27 judge calls, 18,960 tokens,
0 truncated). Items: the 7 default-setting answers released as SUPPORTED → unchanged control + 3 code-made corruptions.

| Item | Result |
|---|---|
| Controls (unchanged) | **0/7 false rejections** |
| Number swap (first number, leading digit +1) | caught **3/4** (n/a for A01, A03, A06: no number) |
| Citation swap (labels re-pointed to a retrieved chunk without the evidence) | caught **7/7** |
| Unsupported addition ("The model was also trained on 512 TPU v4 chips for 3 weeks [S<first label>].") | caught **4/7** |

**Misses:** unsupported addition on **A02, A04, A07** (the judge's reason addressed only the first, true sentence);
number swap on **A07** ("2-stage" → "3-stage" judged SUPPORTED while the judge's reason quoted "2‑stage").

**Weak spot:** a fabricated sentence carrying a valid label, placed after a true sentence, passes BOTH the
deterministic check (it is cited) and the judge (which appears to verify the main claim and stop).

**A01 lead (reworded):** borderline paraphrase, judged consistently on identical text (n=2 texts) — default A01's
"stabilizes the gradients" was SUPPORTED twice (control + lead repeat); baseline A01 attempt 1's "stabilizes the softmax
computation and improves training" was UNSUPPORTED twice (baseline run + lead repeat).

**Phase 5 checkpoint answers (user decisions):**
- **The judge earns its cost for wrong sources** (citation swap 7/7, controls 0/7 false rejections) **but not for
  appended claims** (4/7). Judge prompt and pipeline are NOT changed.
- **A correction should NOT widen retrieval:** the only answerable judge rejection (A01) had its gold evidence
  retrieved (a generation problem, not retrieval); A03 was a retrieval miss that the judge SUPPORTED, so widening on
  rejection would not have triggered for it.

## 2026-11-14 — Phase 5 wide-context run (hybrid 800, top-2) — grades and evidence

Ran on the SAME UTC day's evaluation share (not a fresh day): 120b 26,080 tokens (day total 81,624 / 100K),
20b 9,669 (41,738 / 100K). No 429, no truncated judge (largest judge completion 323 tokens).

- **Wide A03 — qualifies (does not reverse) the checkpoint conclusion "a correction should not widen retrieval":**
  n=1 judge rejection CAUSED BY A RETRIEVAL MISS. At 800/top-2 the retrieved chunks (pp.5–6, pp.15–16) held neither
  the gold p.3 nor the abstract; both attempts were judged UNSUPPORTED and the answer was refused after one correction
  (grade refused_wrongly, no footnote). Widening retrieval on rejection could have helped here; default and baseline
  rejections (A01) had the evidence retrieved.
- **Wide A07 — second "correct fact, different page" case** (after A03 default/baseline): "overtakes the BPE
  Transformer after 30 B training bytes", cited to pp.12–13 (gold p.4 not retrieved); grade wrong (missing
  "perplexity", "tokenized Transformer"), footnote "fact correct, different page".
- Wide A06 overridden wrong → correct (same paraphrase rule); A01, A02, A04, A05 confirmed.

## 2026-11-14 — Phase 6: stand-in and limiter — ASSUMPTIONS (one list)

Code: `standin/server.py` (stand-in), `app/limiter.py` (Limiter, SimClock), `app/aclient.py` (call + retries),
`app/pipeline.py` (`answer_steps` generator shared by the sync pipeline and the async load runner). Tests:
`tests/test_phase6.py` (14, repeated 5× without flakes).

1. **Latency** = resample (first_content_ms, total_ms) from the 131 real 200-responses of the same model and
   prompt-size bucket (<300 / 300–1300 / ≥1300 prompt tokens). No parametric fit; the real tail (max ~2.1 s) is the tail.
2. **Slower under load** = latency × (1 + 0.05 × (in-flight − 1)). **NOT calibrated** (we never ran concurrent real
   calls); this is where the stand-in is least like the real thing.
3. **TPM** bucket per model, refilling continuously at limit/60 per second (measured: reset-tokens = charged / 133.3).
   Admission reserves prompt + max_tokens (Q2 hypothesis, medium-low confidence) capped at the bucket size (Phase 1.2:
   an 8,078 request was accepted on a full 8K bucket), then settles to actual usage on completion.
4. **429** body/headers copied from the real one (type, code, message with Limit/Used/Requested, retry-after rounded up,
   x-ratelimit-*); returned after ~75 ms. RPM = sliding 60 s window (30); RPD/TPD daily counters (1,000 / 200,000).
5. **Content** = replay of the real default-setting outputs and verdicts for the 17 answer questions; unknown questions
   get "NOT_IN_PAPERS" / "supported". So stand-in quality = real quality for those questions; grades carry over only when
   the replayed answer is released unchanged.
6. **Failures**: 5xx and slow-success are injectable (--p5xx, --pslow ×10) but default 0: none observed in 131 real calls.
7. **Prompt tokens** at the stand-in = chars / 3.4 + 80 (measured on logged pipeline prompts).
8. **Time compression**: simulated seconds = wall × speed; sleeps, refill, windows and headers use simulated seconds.
   Local Python overhead (ms) is NOT compressed, so it is inflated ×speed in simulated time (small, stated).
9. **Limiter**: one-at-a-time admission in (priority, arrival) order — judge/correction (priority 0) before new requests
   (1); reserve prompt + max_tokens + 100; refuse early (Shed) if the expected refill wait passes the deadline; settle
   refunds need − actual; header sync only lowers the local level (never trusts more than the provider shows);
   cancellation: queued → nothing reserved, admitted-unsent → full refund, sent → keeps the prompt estimate.
10. **Retries**: only per-minute 429, 5xx, transport errors; never a per-day 429; ≤ 2 per call; wait = max(retry-after,
    1 s × 2^n × jitter 0.5–1.5); global retry budget ≤ 10% of calls + 5; never if the wait passes the deadline;
    every retry re-enters the limiter.

## 2026-11-14 — Phase 7: stand-in load runs (no Groq)

Code: `exp/loadgen.py` (open-loop Poisson arrivals, Zipf question mix over the 17 answer questions, patience 20 s,
time ×20, one stand-in process per scenario, seed 7), `exp/build_results.py` → `results/results.csv` (449 rows: 90 real
+ 359 stand-in), `exp/make_tables.py` → `results/tables.md` (Tables 2a/2b/3 computed ONLY from results.csv).
Pipeline = default setting (hybrid 200 top-8, gate 0.60) via `answer_steps` + Limiter + retry policy; retrieval
precomputed per question (deterministic). Fallback ladder when shed or per-day 429: validated cache (this run) →
extractive (top-2 passages, 0 tokens) → refusal. Cache is used ONLY as a fallback rung (no coalescing) so capacity is
measured honestly.

| Scenario (stand-in) | Offered | Requests | ok % | rate-limited % (served by fallback) | gave up % | peak queue |
|---|---|---|---|---|---|---|
| steady | 2/min, 30 min | 63 | 100.0 | 0.0 | 0.0 | 0 |
| burst | 1/min + 40 in 1 min | 63 | 55.6 | 44.4 (cache 18, extractive 10) | 0.0 | 15 |
| overload | 8/min, 15 min | 133 | 47.4 | 51.9 (cache 36, extractive 33) | 0.8 | 11 |
| tpm_halved (4K TPM) | 2/min, 30 min | 63 | 68.3 | 31.7 (cache 13, extractive 7) | 0.0 | 1 |
| daily_exhausted (6K TPD left) | 2/min, 15 min | 37 | 32.4 | 67.6 (cache 10, extractive 15) | 0.0 | 0 |

- Retries per request = 0.00 everywhere: the limiter learned the limit from the first response's headers and waited or
  shed BEFORE any 429. Section 6 ("one case where retrying made things worse") still needs a deliberate comparison run
  (naive client: no limiter, retry every 429) — not done.
- "Rate-limited" rows were still served (cache/extractive) except 1 given up (overload).
- Patience 20 s, Zipf mix and ×20 compression are assumptions; stand-in slowdown under load is uncalibrated (Phase 6 #2).

**"Fewer chunks" rung — proposal from the numbers (user decides):** do NOT add it for per-minute pressure.
- Capacity: 120b reservation per generation ≈ 2,622 at top-8 → ≈ 3.05 generations/min; top-4 ≈ 1,819 → ≈ 4.40/min (+44%).
- Quality cost (Phase 3, zero tokens): hybrid R@4 27/40 vs R@8 34/40 (−17.5 pts); BM25 R@4 31/40 (−7.5 pts).
- Overload offered 9.2/min ≈ 3× capacity: +44% does not close the gap; the fallback ladder already serves every shed
  request but one. If a rung is ever added, use BM25 top-4, not hybrid top-4.
