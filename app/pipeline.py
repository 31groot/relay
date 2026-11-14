"""Phase 4 pipeline, steps [2]-[8], buffered (no streaming to the user). No retries; 401/403/429 stop the run.

  python app/pipeline.py --go-real "question 1" "question 2" ...   (real Groq calls; key from .env)
"""
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import limiter  # noqa: E402
import llm  # noqa: E402
import prompts  # noqa: E402
import retrieve  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "results" / "phase4_log.jsonl"
GEN_MODEL, JUDGE_MODEL = "openai/gpt-oss-120b", "openai/gpt-oss-20b"
GEN_MAX, JUDGE_MAX = 350, 150
GATE = 0.60          # semantic top-1 cosine at size 200 (Phase 3 checkpoint, indicative)
SIZE, TOP_K = 200, 8
GATE_SIZE = 200   # gate always on size-200 semantic scores (calibration size)
TOKEN_CAP, CALL_CAP = 10_000, 8   # smoke-test budget per model / total calls (NOTES.md)
CHARS_PER_TOKEN_EST = 3.2         # min observed 3.37 on logged pipeline prompts (P4-2 judge) → ~5% margin; was 4.0 (underestimated up to 12%)
TEMPLATE_OVERHEAD = 80            # measured gpt-oss chat-template tokens (probe)
PACING_MARGIN = 100               # tokens of safety on top of prompt + max_tokens when pacing


_INDEX = None


def _index():
    """(chunks, embeddings, embedder), loaded once per process and shared by all settings."""
    global _INDEX
    if _INDEX is None:
        import embed
        chunks, emb = embed.load_index()
        _INDEX = (chunks, emb, embed.Embedder())
    return _INDEX


class Retriever:
    """One retrieval setting: method "hybrid" (RRF over BM25 + semantic) or "bm25", chunk size, top_k.
    The GATE signal is always the semantic top-1 cosine over size-200 chunks (the size the 0.60 gate was calibrated
    on), whatever the setting's method or size, so every setting refuses exactly the same questions."""

    def __init__(self, size=SIZE, method="hybrid", top_k=TOP_K):
        chunks, emb, self.embedder = _index()
        rows = lambda n: np.array([i for i, c in enumerate(chunks) if c["size"] == n])
        self.idx, gate_idx = rows(size), rows(GATE_SIZE)
        self.chunks, self.emb = [chunks[i] for i in self.idx], emb[self.idx]
        self.gate_chunks, self.gate_emb = [chunks[i] for i in gate_idx], emb[gate_idx]
        self.method, self.top_k, self.setting = method, top_k, f"{method} {size} top-{top_k}"
        self.bm25 = retrieve.BM25([c["text"] for c in self.chunks])

    def search(self, question, papers=None):
        q = self.embedder.embed_queries([question])[0]
        by_bm25 = retrieve.rank(self.bm25.scores(question))
        order = by_bm25 if self.method == "bm25" else retrieve.rrf(by_bm25, retrieve.rank(self.emb @ q))
        allowed = [j for j in order if papers is None or self.chunks[j]["paper"] in papers]
        keep = set(allowed[:self.top_k])
        top = [(self.chunks[j], rank) for rank, j in enumerate(order, 1) if j in keep]  # rank over all papers
        gate_sem = self.gate_emb @ q
        gate = max(float(gate_sem[j]) for j, c in enumerate(self.gate_chunks) if papers is None or c["paper"] in papers)
        return top, gate


class Budget:
    """Daily token guard per model (+ total call cap). `used` = tokens already spent today; `caps` per model
    override token_cap (e.g. smoke 10K + an amount borrowed from the development share)."""

    def __init__(self, token_cap=TOKEN_CAP, call_cap=CALL_CAP, used=None, caps=None):
        self.token_cap, self.call_cap, self.caps = token_cap, call_cap, caps or {}
        self.used, self.calls = dict(used or {}), 0

    def cap(self, model):
        return self.caps.get(model, self.token_cap)

    def allows(self, model, worst):
        return self.calls < self.call_cap and self.used.get(model, 0) + worst <= self.cap(model)

    def charge(self, model, tokens):
        self.calls += 1
        self.used[model] = self.used.get(model, 0) + tokens


def prior_usage(log_path, day):
    """Tokens already spent per model on `day` (UTC, YYYY-MM-DD) according to the log: actual usage; a rejected
    429 costs 0; a call with no usage reported for another reason is charged its worst case."""
    used = {}
    if log_path.exists():
        for r in map(json.loads, log_path.read_text().splitlines()):
            if r["type"] == "call" and r["ts"][:10] == day:
                t = (r.get("usage") or {}).get("total_tokens")
                if t is None:
                    t = 0 if r["http_status"] == 429 else r["worst_case"]
                used[r["model"]] = used.get(r["model"], 0) + t
    return used


def normalize_output(text):
    """Citation drift seen from gpt-oss (Phase 4 run 1): zero-width chars inside "[\u200bS1]", U+202F before it,
    and lenticular brackets "【S2】". Drop Unicode format chars (Cf), map every Unicode space (Zs) to " ", 【】 → []."""
    out = (" " if unicodedata.category(ch) == "Zs" else ch for ch in text if unicodedata.category(ch) != "Cf")
    return "".join(out).replace("\u3010", "[").replace("\u3011", "]")


def worst_case(messages, max_tokens):
    chars = sum(len(m["content"]) for m in messages)
    return int(chars / CHARS_PER_TOKEN_EST) + TEMPLATE_OVERHEAD + max_tokens


def cited_labels(text):
    """Labels inside [...] groups that start with S: "[S1]", "[S1][S4]", "[S1, S3]" → [1, 4] etc."""
    out = []
    for group in re.findall(r"\[([^\]]*)\]", text):
        if group.strip().startswith("S"):
            out += [int(n) for n in re.findall(r"S(\d+)", group)]
    return out


def stray_brackets(text):
    """Bracketed spans that are not citations ("[Pdrop = 0.1]"). Logged for Phase 5 counts; never fail an answer."""
    return [g for g in re.findall(r"\[([^\]]*)\]", text) if not re.fullmatch(r"\s*S\d+(\s*,\s*S\d+)*\s*", g)]


def deterministic_check(text, n_sources):
    """→ (ok, reason). Rules: (1) every cited label exists; (2) every claim-bearing paragraph cites;
    (3) output is a cited answer OR exactly NOT_IN_PAPERS."""
    t = text.strip()
    if t == prompts.NOT_IN_PAPERS:
        return True, "refusal"
    if not t:
        return False, "empty output"
    if prompts.NOT_IN_PAPERS in t:
        return False, "NOT_IN_PAPERS mixed with other text"
    bad = [n for n in cited_labels(t) if not 1 <= n <= n_sources]
    if bad:
        return False, f"cited label(s) out of range: {sorted(set(bad))} (sources S1–S{n_sources})"
    for para in (p for p in re.split(r"\n\s*\n", t) if re.search(r"[A-Za-z0-9]", p)):
        if not cited_labels(para):
            return False, f"paragraph without citation: {para.strip()[:60]!r}"
    return True, "ok"


def render(text, chunks):
    """[S3] → [Moshi, §3.3.1 Architecture, p.10]; multi-label groups render each label."""
    def one(n):
        c = chunks[n - 1]
        return f"[{prompts.PAPER_NAMES[c['paper']]}, §{c['section_heading']}, {prompts.pages(c)}]"
    return re.sub(r"\[([^\]]*)\]", lambda m: "".join(one(n) for n in cited_labels(m.group(0)))
                  if m.group(1).strip().startswith("S") else m.group(0), text)


def parse_verdict(text):
    m = re.search(r"\{.*\}", text, re.S)
    try:
        v = json.loads(m.group(0)) if m else None
    except json.JSONDecodeError:
        v = None
    if not isinstance(v, dict) or v.get("verdict") not in ("supported", "unsupported"):
        return {"verdict": "unsupported", "claim": "", "reason": "unparseable judge output", "parsed": False}
    return {"verdict": v["verdict"], "claim": str(v.get("claim", "")), "reason": str(v.get("reason", "")), "parsed": True}


class Pipeline:
    def __init__(self, retriever, client, key, budget=None, url=llm.GROQ_URL, log_path=LOG, pacer=None):
        self.retriever, self.client, self.key, self.url = retriever, client, key, url
        self.budget, self.log_path, self.pacer = budget or Budget(), log_path, pacer or limiter.Pacer()

    def _log(self, row):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            line = json.dumps(row, ensure_ascii=False).replace(self.key, "***")
            f.write(re.sub(r"\borg_[A-Za-z0-9]+", "org_<redacted>", line) + "\n")  # 429 bodies name the org

    def _call(self, rid, step, model, messages, max_tokens):
        """→ row, or None if the budget guard refuses (never exceeds)."""
        worst = worst_case(messages, max_tokens)
        if not self.budget.allows(model, worst):
            self._log({"type": "budget_refused", "request_id": rid, "step": step, "model": model, "worst_case": worst,
                       "used": self.budget.used.get(model, 0), "cap": self.budget.cap(model), "calls": self.budget.calls})
            return None
        waited = self.pacer.wait(model, worst + PACING_MARGIN)
        row = llm.chat(self.client, self.key, model, messages, max_tokens, url=self.url)
        row["pace_wait_s"] = waited
        self.pacer.update(model, row.get("response_headers"))
        total = (row.get("usage") or {}).get("total_tokens")
        self.budget.charge(model, total if total is not None else worst)  # no usage → charge worst case
        self._log({"type": "call", "request_id": rid, "step": step, "ts": datetime.now(timezone.utc).isoformat(),
                   "worst_case": worst, **row})
        if row["http_status"] in llm.STOP_STATUSES:
            raise llm.Stopped(f"HTTP {row['http_status']} at {step}")
        return row

    def answer(self, question, rid):
        res = {"type": "request", "request_id": rid, "question": question, "attempts": []}
        papers = retrieve.route(question)
        top, score = self.retriever.search(question, papers)
        res.update(routed=sorted(papers) if papers else None, broadened=False,
                   retrieval=getattr(self.retriever, "setting", None))
        if score < GATE and papers:  # [4] weak in the routed paper → re-search all papers once before refusing
            top, score = self.retriever.search(question, None)
            res["broadened"] = True
        res.update(gate_score=round(score, 4), retrieved=[{"label": f"S{n}", "chunk_id": c["chunk_id"], "paper": c["paper"],
                   "pages": prompts.pages(c), "rank": int(r)} for n, (c, r) in enumerate(top, 1)])
        if score < GATE:
            return self._finish(res, "refused", "gate", None)
        chunks = [c for c, _ in top]
        gen_msgs = prompts.generator_messages(question, chunks)
        msgs = gen_msgs
        for attempt in (1, 2):  # original + at most one correction, same context
            gen = self._call(rid, f"generate#{attempt}", GEN_MODEL, msgs, GEN_MAX)
            if gen is None:
                return self._finish(res, "refused", "budget", None)
            if gen["http_status"] != 200:
                return self._finish(res, "error", f"provider HTTP {gen['http_status'] or gen.get('error')}", None)
            raw = gen["text"].strip()
            text = normalize_output(raw).strip()  # format drift is fixed here, so it never costs a correction call
            ok, why = deterministic_check(text, len(chunks))
            step = {"attempt": attempt, "raw_output": raw, "normalized_output": text, "check": why, "check_ok": ok,
                    "format_only_fix": ok and not deterministic_check(raw, len(chunks))[0],
                    "stray_brackets": stray_brackets(text),
                    "gen_tokens": (gen.get("usage") or {}).get("total_tokens"), "gen_pace_wait_s": gen["pace_wait_s"]}
            res["attempts"].append(step)
            if ok and text == prompts.NOT_IN_PAPERS:
                return self._finish(res, "refused", "generator: NOT_IN_PAPERS (judge skipped)", None)
            if ok:
                labels = sorted(set(cited_labels(text)))
                step["cited_labels"] = [f"S{n}" for n in labels]
                jr = self._call(rid, f"judge#{attempt}", JUDGE_MODEL,
                                prompts.judge_messages(question, text, [(n, chunks[n - 1]) for n in labels]), JUDGE_MAX)
                if jr is None:
                    return self._finish(res, "refused", "budget", None)
                verdict = parse_verdict(jr["text"]) if jr["http_status"] == 200 else \
                    {"verdict": "unsupported", "claim": "", "reason": f"judge HTTP {jr['http_status']}", "parsed": False}
                step["judge"] = verdict
                step["judge_tokens"] = (jr.get("usage") or {}).get("total_tokens")
                step["judge_pace_wait_s"] = jr["pace_wait_s"]
                if verdict["verdict"] == "supported":
                    return self._finish(res, "answered", "judge: supported", render(text, chunks))
                claim, reason = verdict["claim"], verdict["reason"]
            else:
                claim, reason = "", f"deterministic check failed: {why}"
            if attempt == 1:
                msgs = prompts.correction_messages(gen_msgs, text, claim, reason)
        return self._finish(res, "refused", "failed after one correction", None)

    def _finish(self, res, outcome, reason, rendered):
        res.update(outcome=outcome, outcome_reason=reason, final_answer=rendered, budget_used=dict(self.budget.used),
                   calls_so_far=self.budget.calls)
        self._log(res)
        return res


def show(res):
    """Side-by-side report for one question, for the user to read."""
    print(f"\n{'=' * 100}\n{res['request_id']}: {res['question']}")
    print(f"route: {res['routed'] or 'all papers'}{'  → broadened to all papers' if res['broadened'] else ''}   "
          f"gate score (semantic top-1): {res['gate_score']}  (gate {GATE})")
    for r in res["retrieved"]:
        print(f"   {r['label']:<4}{r['paper']:<10}{r['pages']:<10}rank {r.get('rank', r.get('hybrid_rank')):<4}{r['chunk_id']}")
    for a in res["attempts"]:
        print(f"-- attempt {a['attempt']} raw generator output ({a['gen_tokens']} tokens, paced {a['gen_pace_wait_s']} s):"
              f"\n{json.dumps(a['raw_output'], ensure_ascii=True)}")
        if a["normalized_output"] != a["raw_output"]:
            print(f"-- normalized:\n{a['normalized_output']}")
        print(f"-- deterministic check: {'PASS' if a['check_ok'] else 'FAIL'} ({a['check']})"
              + ("  [format-only: passed only after normalization]" if a["format_only_fix"] else ""))
        if "judge" in a:
            j = a["judge"]
            print(f"-- judge ({a['judge_tokens']} tokens, paced {a['judge_pace_wait_s']} s) saw cited labels "
                  f"{a['cited_labels']}: {j['verdict'].upper()} — {j['reason']}"
                  + (f" | claim: {j['claim']}" if j["claim"] else "") + ("" if j["parsed"] else " [unparseable]"))
    print(f"-- OUTCOME: {res['outcome']} ({res['outcome_reason']})")
    print(f"-- final rendered answer:\n{res['final_answer'] or '(none)'}")
    print(f"-- budget used so far today: {res['budget_used']}, calls this run {res['calls_so_far']}/{CALL_CAP}")


def main(argv):
    if not argv or argv[0] != "--go-real":
        sys.exit("refusing: real Groq calls need --go-real (Phase 4 smoke test)")
    argv, borrow = argv[1:], {}
    while argv and argv[0] == "--borrow-dev":  # --borrow-dev MODEL=TOKENS: extra cap taken from the development share
        model, tokens = argv[1].split("=")
        borrow[model], argv = int(tokens), argv[2:]
    argv = ["--go-real"] + argv
    import httpx
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        sys.exit("GROQ_API_KEY not set")
    try:
        used = prior_usage(LOG, datetime.now(timezone.utc).date().isoformat())
        caps = {m: TOKEN_CAP + borrow.get(m, 0) for m in (GEN_MODEL, JUDGE_MODEL)}
        for m in caps:
            print(f"budget {m}: cap {caps[m]} = smoke {TOKEN_CAP} + borrowed from development {borrow.get(m, 0)}; "
                  f"already used today {used.get(m, 0)}; available {caps[m] - used.get(m, 0)}")
        with httpx.Client() as client:
            p = Pipeline(Retriever(), client, key, budget=Budget(used=used, caps=caps))
            for i, q in enumerate(argv[1:], 1):
                show(p.answer(q, f"P4-{i}"))
    except llm.Stopped as e:
        sys.exit(f"STOPPED: {e} (see {LOG})")
    except Exception as e:  # last resort: never print the key
        sys.exit(f"crashed: {type(e).__name__}: {str(e).replace(key, '***')}")


if __name__ == "__main__":
    main(sys.argv[1:])
