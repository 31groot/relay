# Relay — locked architecture (2026-11-14)

Changes to this file need an explicit decision + reason in the changelog at the bottom.

## Runtime: one request

```
 ┌──────────────────────────── PROC 1: loadgen ────────────────────────────┐
 │ open-loop arrivals (Poisson) · question mix (few hot, many rare)        │
 │ patience timer → disconnect = "given up" · client.csv (TTFT, total)     │
 └───────────────────────────────┬─────────────────────────────────────────┘
                                 │ POST /query  ← SSE: status events…answer
 ┌───────────────────────────────▼──────── PROC 2: app (single process) ───┐
 │                                                                         │
 │  [1] Coalesce / cache ── exact-normalized hit ─────────────► RELEASE    │
 │      (validated answers only; identical in-flight → share one result)   │
 │        │ miss                                                           │
 │  [2] Route (rules: explicit paper names only; else all 3)               │
 │        │                                                                │
 │  [3] Retrieve ── BM25 ─────────────┐                                    │
 │                  bge-small (NumPy) ┴─► RRF                              │
 │        │                                                                │
 │  [4] Abstention gate (raw top score, calibrated)                        │
 │        ├─ weak in routed paper ─► broaden to all 3 ─► [3][4] once more  │
 │        ├─ still weak ──────────────────────────────────────► REFUSE     │
 │        │ pass                                    (off-topic, 0 tokens)  │
 │  [5] Generate (buffered; cites chunk IDs only, e.g. [c42])              │
 │        ├─ "NOT_IN_PAPERS" ─────────────────────────────────► REFUSE     │
 │        │ draft                                                          │
 │  [6] Deterministic check (0 tokens)                                     │
 │        · every cited ID ∈ retrieved set                                 │
 │        · every claim paragraph has ≥1 cite                              │
 │        · output is cited answer OR exactly NOT_IN_PAPERS                │
 │        │ pass                       │ fail (skip judge)                 │
 │  [7] LLM judge (separate model, short verdict + reason)                 │
 │        │ supported    unsupported ──┴─► 1 correction, SAME context      │
 │        │                                 → [6][7] again                 │
 │        │                                     │ still fails              │
 │        ▼                                     ▼                          │
 │  [8] Render citations from chunk metadata    REFUSE                     │
 │      [c42] → [paper, §section, p.N]                                     │
 │        ▼                                                                │
 │     RELEASE (+ store in validated cache)                                │
 │                                                                         │
 │  Fallback when quota/deadline won't allow [5]–[7]:                      │
 │     cached validated → extractive (passes gate [4]) → honest refusal    │
 │     ✗ never an unjudged generated answer                                │
 │                                                                         │
 │  ┌─────── every model call ([5], [7], correction, retries) ──────────┐  │
 │  │ limiter.acquire(est_tokens, deadline, priority)                   │  │
 │  │  · per-model buckets: RPM / TPM / RPD / TPD                       │  │
 │  │  · priority: requests already in progress > new requests          │  │
 │  │  · refuse early if expected wait > remaining deadline             │  │
 │  │  · sync from x-ratelimit-* headers · try/finally release          │  │
 │  │  · retries re-enter limiter (jitter, retry budget)                │  │
 │  │  · client disconnect → cancel upstream call                       │  │
 │  └──────────────────────────────┬────────────────────────────────────┘  │
 │  server.csv (stage timings, tokens, retries, queue wait, judge reason,  │
 │              gold-evidence-in-context flag for eval runs)               │
 └─────────────────────────────────┼───────────────────────────────────────┘
                                   │ httpx, one client, base_url switch
                ┌──────────────────┴──────────────────┐
                ▼                                     ▼
          Groq (real)               PROC 3: stand-in (OpenAI-compatible)
     generator model + judge model  SSE · 429 + headers · TPM/RPD limits ·
     (separate per-model quotas)    slow / fail / slow-success · slower
                                    with load · calibrated from probe
```

## Offline: data and experiments

```
 arXiv PDFs (v7, v1, v2) ─► ingest (PyMuPDF, clean, chunk, 1 tokenizer)
                              └─► chunks.jsonl + embeddings.npy  (per chunk size)

 probe.py (20–30 real calls) ─► latency / limits / 429 shape ─► stand-in config
                                                              ─► Table 1, model pair

 eval_retrieval.py (~40 q, 0 tokens), gold = paper + page + key phrase
     ─► Recall@5 / @10 / @budget per setting ─► pick 3–4 settings
     ─► top-score distributions (answerable vs unanswerable) ─► gate threshold

 eval_answers.py (15+ gold q incl. ~10 unanswerable, real Groq) ─► Table 3
     ─► judge-rejection cause: retrieval miss vs generation error

 judge test (corrupted answers) ─► judge recall / false-rejection rate

 loadgen × 5 scenarios ─► client.csv + server.csv ─► merge ─► results.csv ─► make_tables.py
```

## Key decisions

| Area | Decision | Validated by |
|---|---|---|
| Release | Buffer → validate → release. SSE status events only for cancellation; never counted as first token | TTFT cost = `first_token_ms − generation_first_token_ms` from logs |
| Judge | Every generated answer; separate model; after deterministic check | Recall on corrupted answers; false-rejection rate |
| No quota | Validated exact-match cache → gated extractive → honest refusal. Never unjudged | Scenario: daily budget exhausted |
| Retrieval | BM25, bge-small (NumPy cosine), hybrid RRF. **No reranker** | Recall@5/@10/@budget |
| Abstention | Raw-score gate (off-topic) + `NOT_IN_PAPERS` + judge (near-miss) | Score distributions on unanswerable set |
| Correction | One retry, same context | Gold-evidence-in-context flag on rejections |
| Citations | Model emits chunk IDs; code renders paper/section/page | By construction |
| Client | httpx, one client, `base_url` switches Groq ↔ stand-in | — |
| Load | Open-loop, separate processes, single app process | — |

## Open

- Model pair (generator, judge) — decided by probe.
- "Fewer chunks" degradation rung (still judged) — in or out.
- Whether direct retrieval counts as the brief's "tool call", or the model should call search as a tool.

## Changelog

- 2026-11-14: locked. Reranker dropped (user decision: cut scope; hybrid + RRF only).
- 2026-11-14: PDF extraction PyMuPDF → poppler `pdftotext` (user decision). Reason: already installed, clean reading
  order on all three papers, `-layout` keeps table rows aligned; headings handled by regex instead of font sizes.
  Table pages (a "Table N:" caption) use `-layout`. Known limits: powers of ten flatten (10^20 → "1020");
  plot ticks survive on pages that mix a plot and a table.
- 2026-11-14: chunking (Phase 2.3a) at 200/400/800 generator tokens (4.4 chars/token, measured), 15% overlap,
  never across papers or removed-reference gaps. Known gaps: DC's top-level "§2" heading is not detected (text
  before §2.1 is labelled "1 Introduction"); the last chunk before each gap is short (5–6 per size).
- 2026-11-14: embeddings (Phase 2.3b): bge-small-en-v1.5 via onnxruntime, CLS pooling + normalize; chunks over the
  512-token window split into equal pieces, averaged, renormalized; query instruction on queries only.
  Known gap: DC p7 Figure 2 spells "...new product!" byte by byte ("n e w _ p r o d u c t ! _", 6 lines);
  deliberate figure text, not an extraction defect. Not joined (user decision): the caption and body text
  on p7 contain "new product!" as normal words.
- 2026-11-14: Phase 6 build. Stand-in = stdlib asyncio HTTP server (no FastAPI/uvicorn dependency), OpenAI-compatible
  SSE, calibrated by resampling real latencies. Limiter = async per-model TPM bucket with priority admission, deadline
  shedding, refund-on-settle and header sync (`app/limiter.py`); retries in `app/aclient.py`. Pipeline steps [5]-[8]
  became one generator (`answer_steps`) driven synchronously (Phase 4/5) or under asyncio (load runs). The FastAPI app
  server and SSE to users are NOT built: load runs drive the pipeline in-process (see NOTES.md Phase 7).
