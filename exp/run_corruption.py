"""Phase 5 judge corruption test (gpt-oss-20b, judge max_tokens 400). Generation is never re-run.

  python exp/run_corruption.py --go-real   →  rows appended to results/phase5_log.jsonl (type "corruption"),
                                              summary in results/judge_corruption.csv

Items: every default-setting answer released as SUPPORTED → unchanged control + number_swap + citation_swap +
unsupported_addition (corruptions that do not apply are recorded as not_applicable, no call). Plus two "lead" controls
for the A01 consistency lead: default A01's released answer (a repeat of its standard control) and baseline A01's
attempt-1 answer. Judge sees exactly the chunks the (corrupted) text cites, as in the pipeline.
Expected verdict: controls → supported (else a false rejection); corruptions → unsupported (a catch).
judge_truncated verdicts are excluded from both rates and reported separately. Guard and 401/403/429 as in Phase 5.
"""
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "exp")]
import corrupt  # noqa: E402
import llm  # noqa: E402
import pipeline as pl  # noqa: E402
import prompts  # noqa: E402
import run_phase5 as rp  # noqa: E402

CSV = ROOT / "results" / "judge_corruption.csv"
KINDS = ("control",) + corrupt.CORRUPTIONS


def build_items(log_path, questions, chunks_by_id):
    """→ [{item_id, qid, kind, expected, text, retrieved}] or kind marked not_applicable (text None)."""
    latest, items = rp.latest_requests(log_path), []
    for qid in sorted(questions):
        req = latest.get(f"default:{qid}")
        if not req or req["outcome"] != "answered":
            continue
        text = req["attempts"][-1]["normalized_output"]
        retrieved = [x["chunk_id"] for x in req["retrieved"]]
        variants = {"control": text, "number_swap": corrupt.number_swap(text),
                    "citation_swap": corrupt.citation_swap(text, [chunks_by_id[c]["text"] for c in retrieved],
                                                           questions[qid]["evidence_phrase"]),
                    "unsupported_addition": corrupt.unsupported_addition(text)}
        for kind in KINDS:
            items.append({"item_id": f"corrupt:{qid}:{kind}", "qid": qid, "kind": kind, "text": variants[kind],
                          "expected": "supported" if kind == "control" else "unsupported", "retrieved": retrieved})
    for setting, label, attempt in (("default", "lead_default_A01_released", -1), ("baseline", "lead_baseline_A01_attempt1", 0)):
        req = latest.get(f"{setting}:A01")
        if req:
            items.append({"item_id": f"corrupt:A01:{label}", "qid": "A01", "kind": label, "expected": "contested",
                          "text": req["attempts"][attempt]["normalized_output"], "retrieved": [x["chunk_id"] for x in req["retrieved"]]})
    return items


def done_items(log_path):
    if not log_path.exists():
        return {}
    return {r["item_id"]: r for r in map(json.loads, log_path.read_text().splitlines()) if r["type"] == "corruption"}


def judge_item(pipeline, item, question, chunks_by_id):
    chunks = [chunks_by_id[c] for c in item["retrieved"]]
    labels = sorted(set(n for n in pl.cited_labels(item["text"]) if 1 <= n <= len(chunks)))
    jr = pipeline._call(item["item_id"], "judge#corrupt", pl.JUDGE_MODEL,
                        prompts.judge_messages(question, item["text"], [(n, chunks[n - 1]) for n in labels]), pl.JUDGE_MAX)
    if jr is None:
        return None
    v = pl.judge_verdict(jr)
    row = {"type": "corruption", "ts": datetime.now(timezone.utc).isoformat(), **{k: item[k] for k in ("item_id", "qid", "kind", "expected", "text")},
           "cited_labels": [f"S{n}" for n in labels], "verdict": v["verdict"], "claim": v["claim"], "reason": v["reason"],
           "tokens": (jr.get("usage") or {}).get("total_tokens"), "finish_reason": jr.get("finish_reason")}
    pipeline._log(row)
    return row


def summarize(rows):
    """→ {kind: (caught_or_rejected, n_scored, n_truncated)}. Truncated verdicts never count either way."""
    out = {}
    for kind in KINDS:
        rs = [r for r in rows if r["kind"] == kind and r.get("verdict")]
        trunc = sum(r["verdict"] == "judge_truncated" for r in rs)
        scored = [r for r in rs if r["verdict"] != "judge_truncated"]
        out[kind] = (sum(r["verdict"] == "unsupported" for r in scored), len(scored), trunc)
    return out


def main(argv):
    if argv != ["--go-real"]:
        sys.exit("usage: python exp/run_corruption.py --go-real")
    import httpx
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        sys.exit("GROQ_API_KEY not set")
    qs = {q["id"]: q for q in json.loads((ROOT / "eval" / "questions.json").read_text())["answer_questions"]}
    chunks_by_id = {c["chunk_id"]: c for c in map(json.loads, (ROOT / "data" / "index" / "chunks.jsonl").read_text().splitlines())}
    items = build_items(rp.LOG, qs, chunks_by_id)
    done = done_items(rp.LOG)
    budget = rp.make_budget(rp.LOG, datetime.now(timezone.utc).date().isoformat())
    print(f"{len(items)} items ({sum(i['text'] is None for i in items)} not applicable), {sum(i['item_id'] in done for i in items)} already judged; "
          f"20b used today {budget.used.get(pl.JUDGE_MODEL, 0)} / {rp.EVAL_CAP}")
    try:
        with httpx.Client() as client:
            p = pl.Pipeline(None, client, key, budget=budget, log_path=rp.LOG)
            for item in items:
                if item["text"] is None or item["item_id"] in done:
                    continue
                row = judge_item(p, item, qs[item["qid"]]["question"], chunks_by_id)
                if row is None:
                    print(f"STOP: eval-share guard refused {item['item_id']}")
                    break
                done[item["item_id"]] = row
    except llm.Stopped as e:
        sys.exit(f"STOPPED: {e} (see {rp.LOG})")
    except Exception as e:
        sys.exit(f"crashed: {type(e).__name__}: {str(e).replace(key, '***')}")
    rows = [done.get(i["item_id"], {**i, "verdict": None if i["text"] else "not_applicable"}) for i in items]
    with CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["item_id", "qid", "kind", "expected", "verdict", "claim", "reason", "tokens", "finish_reason", "text"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in w.fieldnames})
    print(f"20b used today {budget.used.get(pl.JUDGE_MODEL, 0)} / {rp.EVAL_CAP}")


if __name__ == "__main__":
    main(sys.argv[1:])
