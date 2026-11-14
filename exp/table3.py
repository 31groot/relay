"""Phase 5 Table 3 from results/phase5_log.jsonl + results/grade_overrides.jsonl (user grade final).

  python exp/table3.py → results/answers.csv, results/table3.csv, printed tables
Timing: service_ms = sum of its calls' total_ms; total_ms = service_ms + TPM pacing waits (the eval ran back-to-back,
so each request waited for the bucket the previous one drained). Local retrieval is not timed. Answers are buffered,
so the user-visible first token = total_ms. Gate refusals make no calls → time not measured; p50/p95 are over the
requests that called a model (p95 = nearest rank = the slowest of that small n).
"""
import csv
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "exp"), str(ROOT / "app")]
import run_phase5 as rp  # noqa: E402
from grade import grade  # noqa: E402

SETTINGS = {"default": "200 tok, 8 pieces, hybrid RRF, no rerank", "baseline": "200 tok, 8 pieces, BM25, no rerank",
            "wide": "800 tok, 2 pieces, hybrid RRF, no rerank"}
FOOTNOTES = {("*", "A02"): "topic overlaps Phase 4 smoke question P4-1",
             ("default", "A01"): "default judge ran at max_tokens 150; truncated judge re-run at 400 (generation reused); "
                                 "tokens include the truncation-caused correction and the re-judge"}


def pctl(xs, p):
    xs = sorted(xs)
    return xs[max(0, math.ceil(p / 100 * len(xs)) - 1)] if xs else None


def user_grades():
    out = {}
    for r in map(json.loads, (ROOT / "results" / "grade_overrides.jsonl").read_text().splitlines()):
        out[(r["setting"], r["qid"])] = r  # last row wins
    return out


def build_rows():
    qs = {q["id"]: q for q in json.loads((ROOT / "eval" / "questions.json").read_text())["answer_questions"]}
    chunks = {c["chunk_id"]: c for c in map(json.loads, (ROOT / "data" / "index" / "chunks.jsonl").read_text().splitlines())}
    log = [json.loads(line) for line in rp.LOG.read_text().splitlines()]
    calls = {}
    for r in log:
        if r["type"] == "call":
            calls.setdefault(r["request_id"], []).append(r)
    latest, ug, rows = rp.latest_requests(rp.LOG), user_grades(), []
    for setting in SETTINGS:
        for qid, q in sorted(qs.items()):
            req, cs = latest[f"{setting}:{qid}"], calls.get(f"{setting}:{qid}", [])
            g = grade(q, req, chunks)
            u = ug.get((setting, qid))
            gens = [c for c in cs if c["step"].startswith("generate")]
            judges = [c for c in cs if "judge" in c["step"]]
            t = round(sum(c["total_ms"] + 1000 * c.get("pace_wait_s", 0) for c in cs), 1) if cs else None
            svc = round(sum(c["total_ms"] for c in cs), 1) if cs else None
            notes = [n for (s, k), n in FOOTNOTES.items() if k == qid and s in ("*", setting)] + ([u["footnote"]] if u and u.get("footnote") else [])
            rows.append({
                "setting": setting, "qid": qid, "subtype": q["subtype"], "expected": q["expected"],
                "outcome": req["outcome"], "outcome_reason": req["outcome_reason"],
                "auto_grade": g["auto_grade"], "final_grade": u["user_grade"] if u else g["auto_grade"],
                "grade_source": "user" if u else "auto", "footnote": "; ".join(notes),
                "calls": len(cs), "corrections": max(0, len(req["attempts"]) - 1),
                "tokens_in": sum((c.get("usage") or {}).get("prompt_tokens", 0) for c in cs),
                "tokens_out": sum((c.get("usage") or {}).get("completion_tokens", 0) for c in cs),
                "tokens_total": sum((c.get("usage") or {}).get("total_tokens", 0) for c in cs),
                "judge_verdicts": "|".join(a["judge"]["verdict"] for a in req["attempts"] if "judge" in a),
                "judge_calls": len(judges), "gold_evidence_in_context": g["evidence_in_context"],
                "first_token_ms": t, "total_ms": t, "service_ms": svc, "rejudged": bool(req.get("rejudged")),
                "generation_first_token_ms": gens[-1]["first_content_ms"] if gens else None,
                "stray_brackets": sum(len(a.get("stray_brackets", [])) for a in req["attempts"]),
                "format_only_fixes": sum(bool(a.get("format_only_fix")) for a in req["attempts"]),
                "gate_score": req["gate_score"]})
    return rows


def write_csv(path, rows):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def table3(rows):
    out = []
    for setting, desc in SETTINGS.items():
        rs = [r for r in rows if r["setting"] == setting]
        n = len(rs)
        cnt = lambda grade_: sum(r["final_grade"] == grade_ for r in rs)
        assert sum(cnt(g) for g in ("correct", "wrong", "refused_wrongly", "refused_correctly")) == n, setting  # every row graded
        timed = [r for r in rs if r["total_ms"] is not None]
        pct = lambda a, b: f"{a}/{b} ({100 * a / b:.1f}%)"
        sub = lambda st: [r for r in rs if r["subtype"] == st]
        out.append({
            "setting": desc, "questions_asked": n,
            "correct_and_cited": pct(cnt("correct"), n), "wrong": pct(cnt("wrong"), n),
            "refused_wrongly": pct(cnt("refused_wrongly"), n), "correctly_refused": pct(cnt("refused_correctly"), n),
            "correctly_refused_off_topic": pct(sum(r["final_grade"] == "refused_correctly" for r in sub("off-topic")), len(sub("off-topic"))),
            "correctly_refused_near_miss": pct(sum(r["final_grade"] == "refused_correctly" for r in sub("near-miss")), len(sub("near-miss"))),
            "tokens_per_question_mean_all17": round(sum(r["tokens_total"] for r in rs) / n),
            "tokens_per_released_answer_mean": round(sum(r["tokens_total"] for r in rs if r["outcome"] == "answered")
                                                     / max(1, sum(r["outcome"] == "answered" for r in rs))),
            "timed_n": len(timed),
            "first_token_p50_p95_ms": f"{pctl([r['first_token_ms'] for r in timed], 50):.0f} / {pctl([r['first_token_ms'] for r in timed], 95):.0f}",
            "total_p50_p95_ms": f"{pctl([r['total_ms'] for r in timed], 50):.0f} / {pctl([r['total_ms'] for r in timed], 95):.0f}",
            "service_only_p50_p95_ms": f"{pctl([r['service_ms'] for r in timed], 50):.0f} / {pctl([r['service_ms'] for r in timed], 95):.0f}",
            "model_first_token_p50_p95_ms": f"{pctl([r['generation_first_token_ms'] for r in timed], 50):.0f} / {pctl([r['generation_first_token_ms'] for r in timed], 95):.0f}",
        })
    return out


def rejection_causes(rows):
    """Every answerable judge rejection / truncation → retrieval miss vs generation error (gold evidence in context?)."""
    out = [{"setting": r["setting"], "qid": r["qid"], "judge_verdicts": "judge_truncated (max 150) → re-judged: " + r["judge_verdicts"],
             "gold_evidence_in_context": r["gold_evidence_in_context"], "cause": "judge limit (truncated), not a rejection of content",
             "final_grade": r["final_grade"]} for r in rows if r["rejudged"]]
    for r in rows:
        if r["expected"] == "answer" and r["judge_verdicts"] and any(v != "supported" for v in r["judge_verdicts"].split("|")):
            cause = "judge limit (truncated)" if "judge_truncated" in r["judge_verdicts"] and "unsupported" not in r["judge_verdicts"] \
                else ("generation error (evidence retrieved)" if r["gold_evidence_in_context"] else "retrieval miss (evidence not retrieved)")
            out.append({"setting": r["setting"], "qid": r["qid"], "judge_verdicts": r["judge_verdicts"],
                        "gold_evidence_in_context": r["gold_evidence_in_context"], "cause": cause, "final_grade": r["final_grade"]})
    return out


def recall_top_k():
    """Zero-cost: recall at top-4 vs top-8 at size 200 from Phase 3 ranks (results/retrieval_eval.csv)."""
    rows = [r for r in csv.DictReader((ROOT / "results" / "retrieval_eval.csv").open()) if r["variant"] == "base" and r["size"] == "200"]
    out = []
    for ret in ("hybrid", "bm25", "semantic"):
        rs = [r for r in rows if r["retriever"] == ret]
        hit = lambda k: sum(1 for r in rs if r["first_hit_rank"] and int(r["first_hit_rank"]) <= k)
        out.append({"retriever": ret, "R@4 (~800 ctx tokens)": f"{hit(4)}/{len(rs)}", "R@8 (~1,600)": f"{hit(8)}/{len(rs)}"})
    return out


def main():
    rows = build_rows()
    write_csv(ROOT / "results" / "answers.csv", rows)
    t3 = table3(rows)
    write_csv(ROOT / "results" / "table3.csv", t3)
    for name, table in (("TABLE 3", t3), ("JUDGE-REJECTION CAUSES", rejection_causes(rows)), ("RECALL AT TOP-4 (zero cost, Phase 3 data)", recall_top_k())):
        print(f"\n### {name}")
        for r in table:
            print("  " + " | ".join(f"{k}: {v}" for k, v in r.items()))
    print("\nFootnotes:")
    for r in rows:
        if r["footnote"]:
            print(f"  {r['setting']} {r['qid']}: {r['footnote']}")


if __name__ == "__main__":
    main()
