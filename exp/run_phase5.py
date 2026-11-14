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
from grade import grade  # noqa: E402

LOG = ROOT / "results" / "phase5_log.jsonl"
SETTINGS = {"default": ("hybrid", 200, 8), "baseline": ("bm25", 200, 8), "wide": ("hybrid", 800, 2)}
EVAL_CAP = 100_000   # 50% evaluation share per model per day (NOTES.md), separate from the smoke budget
CALL_CAP = 80        # per run: 17 questions × at most 4 calls, plus slack
FOOTNOTES = {"A02": "topic overlaps Phase 4 smoke question P4-1 (optimizer + warmup); prompts were not edited after it"}


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
    if len(argv) != 3 or argv[0] != "--go-real" or argv[1] != "--setting" or argv[2] not in SETTINGS:
        sys.exit("usage: python exp/run_phase5.py --go-real --setting default|baseline|wide")
    setting = argv[2]
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
