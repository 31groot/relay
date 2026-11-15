"""Tables 2a, 2b and 3 computed ONLY from results/results.csv (reviewers can recompute them).

  python exp/make_tables.py  → prints markdown, writes results/tables.md
Percentiles: nearest rank (p95/p99 of n < 100 is close to or equal to the slowest request; n is shown).
Duration = span of start times. Offered rate = requests / duration. "Tokens per answer" = mean tokens over requests that
made ≥ 1 model call. "Cost per 1,000 answers" = 1000 × total cost / succeeded requests (Groq on-demand prices).
"""
import csv
import math
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOAD = ["steady", "burst", "overload", "tpm_halved", "daily_exhausted"]
NAIVE = [("burst", "burst_naive"), ("overload", "overload_naive")]


def is_full(r):
    """Full answer: the pipeline's own outcome reached the user (judged answer, a cached copy of one, or a gate /
    NOT_IN_PAPERS refusal). Real rows without a 'served' value count by status."""
    return r["status"] == "ok" or r.get("served") == "cache"


def is_served(r):
    """Served: a full answer, or the zero-token extractive fallback. Capacity refusals and give-ups are not served."""
    return is_full(r) or r.get("served") == "extractive"


def views(rs):
    n = len(rs)
    return {"n": n, "served": sum(map(is_served, rs)), "full": sum(map(is_full, rs)),
            "rate_limited": sum(r["status"] == "rate_limited" for r in rs),
            "rate_limited_no_full": sum(r["status"] == "rate_limited" and not is_full(r) for r in rs),
            "timeout": sum(r["status"] == "timeout" for r in rs), "error": sum(r["status"] == "error" for r in rs)}


def num(x):
    return float(x) if x not in ("", None) else None


def pctl(xs, p):
    xs = sorted(x for x in xs if x is not None)
    return xs[max(0, math.ceil(p / 100 * len(xs)) - 1)] if xs else None


def fmt3(xs, ps=(50, 95, 99)):
    vals = [pctl(xs, p) for p in ps]
    return " / ".join("–" if v is None else f"{v:.0f}" for v in vals) + f" (n={sum(x is not None for x in xs)})"


def pct(a, n):
    return f"{100 * a / n:.1f}" if n else "–"


def groups(rows):
    g = OrderedDict()
    order = [("steady16", "real"), ("steady16", "stand-in")] + [(s, "stand-in") for s in LOAD + [b for _, b in NAIVE]] + [("probe", "real"), ("smoke", "real"),
             ("quality_default", "real"), ("quality_baseline", "real"), ("quality_wide", "real")]
    for key in order:
        g[key] = [r for r in rows if (r["scenario"], r["source"]) == key]
    return g


def table2(rows):
    a = ["| Scenario | Real or stand-in | Requests sent | Duration (s) | Offered rate (per min) | First token p50 / p95 / p99 (ms) | Total time p50 / p95 / p99 (ms) |",
         "|---|---|---|---|---|---|---|"]
    b = ["| Scenario | Succeeded % (served / full answer) | Rate-limited % (status / without a full answer) | Timed out or given up % | Retries per request | Peak queue length | Tokens per answer | Cost per 1,000 answers (served / full answer) |",
         "|---|---|---|---|---|---|---|---|"]
    for (sc, src), rs in groups(rows).items():
        if not rs:
            a.append(f"| {sc} | {src} | not measured | not measured | not measured | not measured | not measured |")
            b.append(f"| {sc} | not measured | not measured | not measured | not measured | not measured | not measured | not measured |")
            continue
        ts = sorted(datetime.fromisoformat(r["start_time"]) for r in rs if r["start_time"])
        dur = (ts[-1] - ts[0]).total_seconds() if len(ts) > 1 else 0
        rate = f"{len(rs) / (dur / 60):.1f}" if dur else "–"
        timed = [r for r in rs if r["status"] != "error"]   # a failed connection is not a fast response
        a.append(f"| {sc} | {src} | {len(rs)} | {dur:.0f} | {rate} | {fmt3([num(r['first_token_ms']) for r in timed])} | "
                 f"{fmt3([num(r['total_ms']) for r in timed])} |")
        v = views(rs)
        n = v["n"]
        called = [r for r in rs if int(r["tokens_in"] or 0) > 0]
        tok = f"{sum(int(r['tokens_in']) + int(r['tokens_out']) for r in called) / len(called):.0f}" if called else "0"
        q = [int(r["queue_len_at_start"]) for r in rs if r["queue_len_at_start"] not in ("", None)]
        cost = sum(float(r["cost_usd"] or 0) for r in rs)
        per = lambda k: f"${1000 * cost / v[k]:.2f}" if v[k] else "–"
        b.append(f"| {sc} | {pct(v['served'], n)} / {pct(v['full'], n)} | {pct(v['rate_limited'], n)} / {pct(v['rate_limited_no_full'], n)} | "
                 f"{pct(v['timeout'], n)} | {sum(int(r['retries'] or 0) for r in rs) / n:.2f} | "
                 f"{max(q) if q else 'not measured'} | {tok} | {per('served')} / {per('full')} |")
    return a, b


def table3(rows):
    t = ["| Setting (piece size, pieces fetched, re-rank yes or no) | Questions asked | Correct and cited % | Wrong % | Correctly refused % | Tokens per answer | First token p50 / p95 (ms) | Total time p50 / p95 (ms) |",
         "|---|---|---|---|---|---|---|---|"]
    for sc in ("quality_default", "quality_baseline", "quality_wide"):
        rs = [r for r in rows if r["scenario"] == sc]
        n = len(rs)
        g = lambda x: sum(r["answer_grade"] == x for r in rs)
        called = [r for r in rs if int(r["tokens_in"] or 0) > 0]
        tok = sum(int(r["tokens_in"]) + int(r["tokens_out"]) for r in called) / len(called)
        t.append(f"| {rs[0]['retrieval_setting']} | {n} | {pct(g('correct'), n)} | {pct(g('wrong'), n)} | {pct(g('refused_correctly'), n)} | "
                 f"{tok:.0f} | {fmt3([num(r['first_token_ms']) for r in rs], (50, 95))} | {fmt3([num(r['total_ms']) for r in rs], (50, 95))} |")
    return t


def retry_comparison(rows):
    t = ["| Load (same arrivals) | Client | Requests | Served % / full answer % | Given up % | Retries (total / per request) | Rate-limited % | Total time p50 / p95 (ms) | Tokens spent | Wasted tokens (measured) | Calls cancelled mid-flight |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    verdicts = []
    for lim_sc, naive_sc in NAIVE:
        stats = {}
        for sc, client in ((lim_sc, "limiter + backoff + retry budget"), (naive_sc, "naive: retry every 429 immediately")):
            rs = [r for r in rows if r["scenario"] == sc]
            v, tot = views(rs), [num(r["total_ms"]) for r in rs]
            retries = sum(int(r["retries"] or 0) for r in rs)
            spent = sum(int(r["tokens_in"]) + int(r["tokens_out"]) for r in rs)
            wasted = sum(int(r["wasted_tokens"] or 0) for r in rs)
            stats[client] = v
            t.append(f"| {lim_sc} | {client} | {v['n']} | {pct(v['served'], v['n'])} / {pct(v['full'], v['n'])} | {pct(v['timeout'], v['n'])} | "
                     f"{retries} / {retries / v['n']:.2f} | {pct(v['rate_limited'], v['n'])} | {pctl(tot, 50):.0f} / {pctl(tot, 95):.0f} | "
                     f"{spent} | {wasted} | {sum(int(r['cancelled_calls'] or 0) for r in rs)} |")
        a, b = stats.values()
        verdicts.append(f"{lim_sc}: naive retrying made it WORSE — users who gave up {b['timeout']}/{b['n']} vs {a['timeout']}/{a['n']}, "
                        f"served {pct(b['served'], b['n'])}% vs {pct(a['served'], a['n'])}%"
                        if b["timeout"] > a["timeout"] or b["served"] < a["served"] else f"{lim_sc}: naive retrying did NOT make it worse")
    return t, verdicts


def main():
    rows = list(csv.DictReader((ROOT / "results" / "results.csv").open()))
    a, b = table2(rows)
    rc, verdicts = retry_comparison(rows)
    md = ["## Table 2a: Latency", *a, "", "## Table 2b: Outcomes", *b, "",
          "## Retry comparison (stand-in, same seeded arrivals and question sequence)", *rc, "", *[f"- {x}" for x in verdicts], "",
          "## Table 3: Retrieval settings (real, 17 answer questions)", *table3(rows),
          "", "Definitions: full answer = the pipeline's own outcome reached the user (judged answer, cached copy of one, or a gate/"
              "NOT_IN_PAPERS refusal); served = full answer or the zero-token extractive fallback. Wasted tokens = measured usage on "
              "requests that ended without their own outcome; calls cancelled mid-flight are counted, not estimated.",
          "", "Notes: stand-in times are simulated (time compressed ×20, converted back). Real quality-run times include TPM pacing "
              "waits (runs were back-to-back); gate refusals make no model call and are not timed. Latency percentiles exclude status=error rows (the real steady run had 2 local connection failures, ~4 ms each); outcome rates include them. Refused-wrongly % = 100 − correct − "
              "wrong − correctly refused. Percentiles are nearest-rank; with n < 100, p99 is the slowest request."]
    text = "\n".join(md)
    (ROOT / "results" / "tables.md").write_text(text + "\n")
    doc = ROOT / "docs" / "writeup.md"   # keep the write-up's tables identical to the CSV-generated ones
    if doc.exists():
        d = doc.read_text()
        start, end = "<!-- BEGIN GENERATED TABLES -->", "<!-- END GENERATED TABLES -->"
        if start in d and end in d:
            doc.write_text(d[:d.index(start) + len(start)] + "\n" + text + "\n" + d[d.index(end):])
    print(text)


if __name__ == "__main__":
    main()
