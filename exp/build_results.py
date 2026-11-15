"""Merge every request (real + stand-in) into results/results.csv — the file all tables are computed from.

Mandatory columns (brief): request_id, scenario, source, start_time, first_token_ms, total_ms, status, retries,
tokens_in, tokens_out, retrieval_setting, answer_grade.  Extra: qid, served, outcome_reason, queue_len_at_start, cost_usd, client, wasted_tokens, cancelled_calls.
Real sources: probe (32 single calls), Phase 4 smoke (2 runs), Phase 5 quality runs (3 settings × 17).
Stand-in source: results/load_runs.jsonl (exp/loadgen.py). Answers are buffered → first_token_ms = total_ms for requests.
"""
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "exp")]
import table3  # noqa: E402

COLS = ["request_id", "scenario", "source", "start_time", "first_token_ms", "total_ms", "status", "retries", "tokens_in",
        "tokens_out", "retrieval_setting", "answer_grade", "qid", "served", "outcome_reason", "queue_len_at_start", "cost_usd",
        "client", "wasted_tokens", "cancelled_calls"]
PRICES = {"openai/gpt-oss-120b": (0.15, 0.60), "openai/gpt-oss-20b": (0.075, 0.30), "qwen/qwen3.8-27b": (0.80, 4.00)}


def cost(calls):
    return round(sum(((c.get("usage") or {}).get("prompt_tokens", 0) * PRICES[c["model"]][0]
                      + (c.get("usage") or {}).get("completion_tokens", 0) * PRICES[c["model"]][1]) / 1e6 for c in calls), 8)


def status_of(http):
    return "ok" if http == 200 else ("rate_limited" if http == 429 else ("timeout" if http is None else "error"))


def probe_rows():
    for r in map(json.loads, (ROOT / "results" / "probe_log.jsonl").read_text().splitlines()):
        u = r.get("usage") or {}
        yield {"request_id": f"probe-{r['call_no']:02d}", "scenario": "probe", "source": "real", "start_time": r["ts"],
               "first_token_ms": r["first_content_ms"], "total_ms": r["total_ms"], "status": status_of(r["http_status"]),
               "retries": 0, "tokens_in": u.get("prompt_tokens", 0), "tokens_out": u.get("completion_tokens", 0),
               "retrieval_setting": "none (single call)", "answer_grade": "", "qid": "", "served": "",
               "outcome_reason": f"{r['model']} {r['prompt_kind']}", "queue_len_at_start": "", "cost_usd": cost([r])}


def smoke_rows():
    rows, run, pending = list(map(json.loads, (ROOT / "results" / "phase4_log.jsonl").read_text().splitlines())), 1, []
    for r in rows:
        if r["type"] == "call":
            pending.append(r)
            if r["http_status"] == 429:          # run 1 stopped here: record the request as rate-limited, start run 2
                yield smoke_row(f"smoke1-{r['request_id']}", pending, "rate_limited", "stopped: HTTP 429 (TPM)")
                pending, run = [], 2
        elif r["type"] == "request":
            yield smoke_row(f"smoke{run}-{r['request_id']}", pending, "ok", f"{r['outcome']}: {r['outcome_reason']}", r)
            pending = []


def smoke_row(rid, calls, status, reason, req=None):
    t = round(sum(c["total_ms"] + 1000 * c.get("pace_wait_s", 0) for c in calls), 1) if calls else None
    return {"request_id": rid, "scenario": "smoke", "source": "real", "start_time": calls[0]["ts"] if calls else "",
            "first_token_ms": t if status == "ok" else None, "total_ms": t, "status": status, "retries": 0,
            "tokens_in": sum((c.get("usage") or {}).get("prompt_tokens", 0) for c in calls),
            "tokens_out": sum((c.get("usage") or {}).get("completion_tokens", 0) for c in calls),
            "retrieval_setting": table3.SETTINGS["default"], "answer_grade": "", "qid": "", "served": "",
            "outcome_reason": reason, "queue_len_at_start": "", "cost_usd": cost(calls)}


def quality_rows():
    log = [json.loads(line) for line in (ROOT / "results" / "phase5_log.jsonl").read_text().splitlines()]
    calls, last_ts, first_ts = {}, {}, None
    for r in log:                                   # start_time: first call of the request, else the latest call before it
        if r["type"] == "call":
            calls.setdefault(r["request_id"], []).append(r)
            first_ts = r["ts"]
        elif r["type"] == "request":
            last_ts.setdefault(r["request_id"], first_ts)
    for a in table3.build_rows():
        rid = f"{a['setting']}:{a['qid']}"
        cs = calls.get(rid, [])
        yield {"request_id": f"quality-{a['setting']}-{a['qid']}", "scenario": f"quality_{a['setting']}", "source": "real",
               "start_time": cs[0]["ts"] if cs else last_ts.get(rid, ""), "first_token_ms": a["first_token_ms"],
               "total_ms": a["total_ms"], "status": "error" if a["outcome"] == "error" else "ok", "retries": 0,
               "tokens_in": a["tokens_in"], "tokens_out": a["tokens_out"], "retrieval_setting": table3.SETTINGS[a["setting"]],
               "answer_grade": a["final_grade"], "qid": a["qid"], "served": "answer" if a["outcome"] == "answered" else "refusal",
               "outcome_reason": a["outcome_reason"], "queue_len_at_start": "", "cost_usd": cost(cs)}


def steady_rows(path, scenario, source):
    """exp/run_steady_real.py logs → one row per finished request (budget-refused requests are not finished)."""
    if not path.exists():
        return
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    calls = {}
    for r in rows:
        if r["type"] == "call":
            calls.setdefault(r["request_id"], []).append(r)
    for r in rows:
        if r["type"] != "request" or r["status"] == "budget":
            continue
        cs = calls.get(r["request_id"], [])
        yield {"request_id": f"{scenario}-{r['request_id']}", "scenario": scenario, "source": source, "start_time": r["start_time"],
               "first_token_ms": r["first_token_ms"], "total_ms": r["total_ms"], "status": r["status"], "retries": 0,
               "tokens_in": sum((c.get("usage") or {}).get("prompt_tokens", 0) for c in cs),
               "tokens_out": sum((c.get("usage") or {}).get("completion_tokens", 0) for c in cs),
               "retrieval_setting": table3.SETTINGS["default"], "answer_grade": r["answer_grade"], "qid": r["qid"],
               "served": r["served"], "outcome_reason": r["outcome_reason"], "queue_len_at_start": "", "cost_usd": cost(cs),
               "client": "limiter, no retries (stop on 401/403/429)", "wasted_tokens": "", "cancelled_calls": ""}


def load_rows():
    path = ROOT / "results" / "load_runs.jsonl"
    for r in map(json.loads, path.read_text().splitlines()) if path.exists() else []:
        yield {c: r.get(c, "") for c in COLS}


def main():
    rows = list(probe_rows()) + list(smoke_rows()) + list(quality_rows()) + list(load_rows()) \
        + list(steady_rows(ROOT / "results" / "steady_standin_prediction_log.jsonl", "steady16", "stand-in")) \
        + list(steady_rows(ROOT / "results" / "steady_real_log.jsonl", "steady16", "real"))
    ids = [r["request_id"] for r in rows]
    assert len(ids) == len(set(ids)), "duplicate request_id"
    with (ROOT / "results" / "results.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    by = {}
    for r in rows:
        by[(r["scenario"], r["source"])] = by.get((r["scenario"], r["source"]), 0) + 1
    print(f"results/results.csv: {len(rows)} rows", by)


if __name__ == "__main__":
    main()
