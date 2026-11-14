"""Phase 5 runner: one retrieval setting over the 17 answer questions, with resume.

  python exp/run_phase5.py --go-real --setting default|baseline|wide

Resume: (setting, question) pairs with a finished request row in results/phase5_log.jsonl are skipped; a request
refused for budget or ended by an error is NOT finished and runs again. The eval-share guard (100K per model per UTC
day) counts tokens from the same log, so it holds across resumes. 401/403/429 stop the run (no retries).
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "exp")]
import llm  # noqa: E402
import pipeline as pl  # noqa: E402
import prompts  # noqa: E402
from grade import grade  # noqa: E402

LOG = ROOT / "results" / "phase5_log.jsonl"
SETTINGS = {"default": ("hybrid", 200, 8), "baseline": ("bm25", 200, 8), "wide": ("hybrid", 800, 2)}
EVAL_CAP = 100_000   # 50% evaluation share per model per day (NOTES.md), separate from the smoke budget
CALL_CAP = 80        # per run: 17 questions × at most 4 calls, plus slack
FOOTNOTES = {"A02": "topic overlaps Phase 4 smoke question P4-1 (optimizer + warmup); prompts were not edited after it"}
SETTING_NOTES = {"default": "ran with judge max_tokens 150; only items whose judge was truncated were re-judged with 400 "
                            "(logged generation reused, no new generation)"}


def latest_requests(log_path):
    """request_id → its LAST request row (a re-judge row supersedes the original)."""
    out = {}
    for r in map(json.loads, log_path.read_text().splitlines()):
        if r["type"] == "request":
            out[r["request_id"]] = r
    return out


def truncated_judges(log_path, setting):
    """request_ids of this setting whose latest row came from a judge call cut off by max_tokens."""
    rows = list(map(json.loads, log_path.read_text().splitlines()))
    hit = {r["request_id"] for r in rows if r["type"] == "call" and r["step"].startswith("judge")
           and r.get("finish_reason") == "length" and r["request_id"].startswith(f"{setting}:")}
    return [rid for rid, req in latest_requests(log_path).items() if rid in hit and not req.get("rejudged")]


def rejudge(pipeline, req, chunks_by_id):
    """Re-run ONLY the truncated judge of attempt 1 on the logged generation (no new generation). → new request row.
    supported → attempt 1 is released (the correction would not have happened); unsupported → the logged correction
    path stands; truncated again → refused as judge_truncated."""
    rid, a1 = req["request_id"], dict(req["attempts"][0])
    chunks = [chunks_by_id[x["chunk_id"]] for x in req["retrieved"]]
    labels = [int(lab[1:]) for lab in a1["cited_labels"]]
    jr = pipeline._call(rid, "rejudge#1", pl.JUDGE_MODEL,
                        prompts.judge_messages(req["question"], a1["normalized_output"], [(n, chunks[n - 1]) for n in labels]),
                        pl.JUDGE_MAX)
    if jr is None:
        return None
    new = {k: v for k, v in req.items() if k not in ("outcome", "outcome_reason", "final_answer", "budget_used", "calls_so_far")}
    a1.update(judge_old=a1["judge"], judge=pl.judge_verdict(jr), judge_tokens=(jr.get("usage") or {}).get("total_tokens"),
              judge_pace_wait_s=jr["pace_wait_s"])
    new.update(rejudged=True, setting_note=SETTING_NOTES.get(rid.split(":")[0]))
    v = a1["judge"]["verdict"]
    if v == "supported":
        new["attempts"] = [a1]
        return pipeline._finish(new, "answered", "judge: supported (re-judged with max 400)", pl.render(a1["normalized_output"], chunks))
    if v == "judge_truncated":
        new["attempts"] = [a1]
        return pipeline._finish(new, "refused", "judge_truncated (answer not verified, not released)", None)
    new["attempts"] = [a1] + req["attempts"][1:]
    return pipeline._finish(new, req["outcome"], req["outcome_reason"] + " (attempt-1 judge re-run: unsupported)", req["final_answer"])


def finished(log_path):
    if not log_path.exists():
        return set()
    return {r["request_id"] for r in map(json.loads, log_path.read_text().splitlines())
            if r["type"] == "request" and r["outcome"] != "error" and r["outcome_reason"] != "budget"}


def pending(questions, setting, log_path):
    done = finished(log_path)
    return [q for q in questions if f"{setting}:{q['id']}" not in done]


def make_budget(log_path, day):
    return pl.Budget(token_cap=EVAL_CAP, call_cap=CALL_CAP, used=pl.prior_usage(log_path, day))


def run(setting, questions, pipeline, log_path, chunks_by_id, out=print):
    """→ list of (question, result, grade). Stops on budget refusal (later questions would be refused too)."""
    results = []
    for q in pending(questions, setting, log_path):
        res = pipeline.answer(q["question"], f"{setting}:{q['id']}")
        if res["outcome_reason"] == "budget":
            out(f"STOP: eval-share budget guard refused {q['id']} (not counted as finished; resumes next run)")
            break
        g = grade(q, res, chunks_by_id)
        results.append((q, res, g))
        pl.show(res)
        out(f"-- expected: {q['expected']} ({q['subtype']})   AUTO-GRADE: {g['auto_grade']}"
            + (f"   missing terms: {g['missing_terms']}" if g["missing_terms"] else "")
            + (f"   cites evidence page: {g['cites_evidence_page']}" if g["cites_evidence_page"] is not None else "")
            + f"   gold evidence in context: {g['evidence_in_context']}"
            + (f"\n-- footnote: {FOOTNOTES[q['id']]}" if q["id"] in FOOTNOTES else ""))
    return results


def main(argv):
    ok = len(argv) == 3 and argv[0] == "--go-real" and argv[1] in ("--setting", "--rejudge-truncated") and argv[2] in SETTINGS
    if not ok:
        sys.exit("usage: python exp/run_phase5.py --go-real --setting|--rejudge-truncated default|baseline|wide")
    mode, setting = argv[1], argv[2]
    import httpx
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        sys.exit("GROQ_API_KEY not set")
    questions = json.loads((ROOT / "eval" / "questions.json").read_text())["answer_questions"]
    chunks_by_id = {c["chunk_id"]: c for c in map(json.loads, (ROOT / "data" / "index" / "chunks.jsonl").read_text().splitlines())}
    budget = make_budget(LOG, datetime.now(timezone.utc).date().isoformat())
    method, size, k = SETTINGS[setting]
    print(f"setting {setting}: {method} {size} top-{k}; gate = semantic top-1 (size-200) >= {pl.GATE}; "
          f"{len(pending(questions, setting, LOG))}/{len(questions)} questions pending")
    for m in (pl.GEN_MODEL, pl.JUDGE_MODEL):
        print(f"eval share {m}: cap {EVAL_CAP}, used today (from {LOG.name}) {budget.used.get(m, 0)}")
    try:
        with httpx.Client() as client:
            if mode == "--rejudge-truncated":
                p = pl.Pipeline(None, client, key, budget=budget, log_path=LOG)  # no retrieval: reuses logged context
                latest = latest_requests(LOG)
                for rid in truncated_judges(LOG, setting):
                    old = latest[rid]
                    new = rejudge(p, old, chunks_by_id)
                    if new is None:
                        print(f"STOP: eval-share guard refused the re-judge of {rid}")
                        break
                    o, n = old["attempts"][0], new["attempts"][0]
                    print(f"{rid}: OLD judge {o['judge']['verdict']} ({o.get('judge_tokens')} tok) → NEW judge {n['judge']['verdict']} "
                          f"({n['judge_tokens']} tok): {n['judge']['reason']}\n   outcome {old['outcome']} ({old['outcome_reason']}) → "
                          f"{new['outcome']} ({new['outcome_reason']})\n   final: {new['final_answer']}")
            else:
                p = pl.Pipeline(pl.Retriever(size, method, k), client, key, budget=budget, log_path=LOG)
                run(setting, questions, p, LOG, chunks_by_id)
    except llm.Stopped as e:
        sys.exit(f"STOPPED: {e} (see {LOG})")
    except Exception as e:  # never print the key
        sys.exit(f"crashed: {type(e).__name__}: {str(e).replace(key, '***')}")
    for m in (pl.GEN_MODEL, pl.JUDGE_MODEL):
        print(f"eval share {m}: used today {budget.used.get(m, 0)} / {EVAL_CAP}")


if __name__ == "__main__":
    main(sys.argv[1:])
