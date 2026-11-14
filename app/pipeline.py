"""Phase 4 pipeline, steps [2]-[8], buffered (no streaming to the user). No retries; 401/403/429 stop the run.

  python app/pipeline.py --go-real "question 1" "question 2" ...   (real Groq calls; key from .env)
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import llm  # noqa: E402
import prompts  # noqa: E402
import retrieve  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "results" / "phase4_log.jsonl"
GEN_MODEL, JUDGE_MODEL = "openai/gpt-oss-120b", "openai/gpt-oss-20b"
GEN_MAX, JUDGE_MAX = 350, 150
GATE = 0.60          # semantic top-1 cosine at size 200 (Phase 3 checkpoint, indicative)
SIZE, TOP_K = 200, 8
TOKEN_CAP, CALL_CAP = 10_000, 8   # smoke-test budget per model / total calls (NOTES.md)
CHARS_PER_TOKEN_EST = 4.0         # budget estimate; measured 4.4 → ~10% margin
TEMPLATE_OVERHEAD = 80            # measured gpt-oss chat-template tokens (probe)


class Retriever:
    """Hybrid (RRF over BM25 + semantic), size-200 chunks. Gate signal = semantic top-1 cosine among searched papers."""

    def __init__(self):
        import embed
        chunks, emb = embed.load_index()
        self.idx = np.array([i for i, c in enumerate(chunks) if c["size"] == SIZE])
        self.chunks, self.emb = [chunks[i] for i in self.idx], emb[self.idx]
        self.bm25, self.embedder = retrieve.BM25([c["text"] for c in self.chunks]), embed.Embedder()

    def search(self, question, papers=None):
        sem = self.emb @ self.embedder.embed_queries([question])[0]
        fused = retrieve.rrf(retrieve.rank(self.bm25.scores(question)), retrieve.rank(sem))
        allowed = [j for j in fused if papers is None or self.chunks[j]["paper"] in papers]
        keep = set(allowed[:TOP_K])
        top = [(self.chunks[j], rank) for rank, j in enumerate(fused, 1) if j in keep]  # rank = hybrid rank over all papers
        return top, float(max(sem[j] for j in allowed))


class Budget:
    def __init__(self, token_cap=TOKEN_CAP, call_cap=CALL_CAP):
        self.token_cap, self.call_cap, self.used, self.calls = token_cap, call_cap, {}, 0

    def allows(self, model, worst):
        return self.calls < self.call_cap and self.used.get(model, 0) + worst <= self.token_cap

    def charge(self, model, tokens):
        self.calls += 1
        self.used[model] = self.used.get(model, 0) + tokens


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
    def __init__(self, retriever, client, key, budget=None, url=llm.GROQ_URL, log_path=LOG):
        self.retriever, self.client, self.key, self.url = retriever, client, key, url
        self.budget, self.log_path = budget or Budget(), log_path

    def _log(self, row):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(json.dumps(row, ensure_ascii=False).replace(self.key, "***") + "\n")

    def _call(self, rid, step, model, messages, max_tokens):
        """→ row, or None if the budget guard refuses (never exceeds)."""
        worst = worst_case(messages, max_tokens)
        if not self.budget.allows(model, worst):
            self._log({"type": "budget_refused", "request_id": rid, "step": step, "model": model, "worst_case": worst,
                       "used": self.budget.used.get(model, 0), "calls": self.budget.calls})
            return None
        row = llm.chat(self.client, self.key, model, messages, max_tokens, url=self.url)
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
        res.update(routed=sorted(papers) if papers else None, broadened=False)
        if score < GATE and papers:  # [4] weak in the routed paper → re-search all papers once before refusing
            top, score = self.retriever.search(question, None)
            res["broadened"] = True
        res.update(gate_score=round(score, 4), retrieved=[{"label": f"S{n}", "chunk_id": c["chunk_id"], "paper": c["paper"],
                   "pages": prompts.pages(c), "hybrid_rank": int(r)} for n, (c, r) in enumerate(top, 1)])
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
            text = gen["text"].strip()
            ok, why = deterministic_check(text, len(chunks))
            step = {"attempt": attempt, "raw_output": text, "check": why, "check_ok": ok}
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
        print(f"   {r['label']:<4}{r['paper']:<10}{r['pages']:<10}hybrid rank {r['hybrid_rank']:<4}{r['chunk_id']}")
    for a in res["attempts"]:
        print(f"-- attempt {a['attempt']} raw generator output:\n{a['raw_output']}")
        print(f"-- deterministic check: {'PASS' if a['check_ok'] else 'FAIL'} ({a['check']})")
        if "judge" in a:
            j = a["judge"]
            print(f"-- judge saw cited labels {a['cited_labels']}: {j['verdict'].upper()} — {j['reason']}"
                  + (f" | claim: {j['claim']}" if j["claim"] else "") + ("" if j["parsed"] else " [unparseable]"))
    print(f"-- OUTCOME: {res['outcome']} ({res['outcome_reason']})")
    print(f"-- final rendered answer:\n{res['final_answer'] or '(none)'}")
    print(f"-- budget used so far: {res['budget_used']}, calls {res['calls_so_far']}/{CALL_CAP}")


def main(argv):
    if not argv or argv[0] != "--go-real":
        sys.exit("refusing: real Groq calls need --go-real (Phase 4 smoke test)")
    import httpx
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        sys.exit("GROQ_API_KEY not set")
    try:
        with httpx.Client() as client:
            p = Pipeline(Retriever(), client, key)
            for i, q in enumerate(argv[1:], 1):
                show(p.answer(q, f"P4-{i}"))
    except llm.Stopped as e:
        sys.exit(f"STOPPED: {e} (see {LOG})")
    except Exception as e:  # last resort: never print the key
        sys.exit(f"crashed: {type(e).__name__}: {str(e).replace(key, '***')}")


if __name__ == "__main__":
    main(sys.argv[1:])
