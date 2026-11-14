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
    order = [("steady", "real")] + [(s, "stand-in") for s in LOAD] + [("probe", "real"), ("smoke", "real"),
             ("quality_default", "real"), ("quality_baseline", "real"), ("quality_wide", "real")]
    for key in order:
        g[key] = [r for r in rows if (r["scenario"], r["source"]) == key]
    return g


def table2(rows):
    a = ["| Scenario | Real or stand-in | Requests sent | Duration (s) | Offered rate (per min) | First token p50 / p95 / p99 (ms) | Total time p50 / p95 / p99 (ms) |",
         "|---|---|---|---|---|---|---|"]
    b = ["| Scenario | Succeeded % | Rate-limited % | Timed out or given up % | Retries per request | Peak queue length | Tokens per answer | Cost per 1,000 answers |",
         "|---|---|---|---|---|---|---|---|"]
    for (sc, src), rs in groups(rows).items():
        if not rs:
            a.append(f"| {sc} | {src} | not measured | not measured | not measured | not measured | not measured |")
            b.append(f"| {sc} | not measured | not measured | not measured | not measured | not measured | not measured | not measured |")
            continue
        ts = sorted(datetime.fromisoformat(r["start_time"]) for r in rs if r["start_time"])
        dur = (ts[-1] - ts[0]).total_seconds() if len(ts) > 1 else 0
        rate = f"{len(rs) / (dur / 60):.1f}" if dur else "–"
        a.append(f"| {sc} | {src} | {len(rs)} | {dur:.0f} | {rate} | {fmt3([num(r['first_token_ms']) for r in rs])} | "
                 f"{fmt3([num(r['total_ms']) for r in rs])} |")
        n, ok = len(rs), sum(r["status"] == "ok" for r in rs)
        called = [r for r in rs if int(r["tokens_in"] or 0) > 0]
        tok = f"{sum(int(r['tokens_in']) + int(r['tokens_out']) for r in called) / len(called):.0f}" if called else "0"
        q = [int(r["queue_len_at_start"]) for r in rs if r["queue_len_at_start"] not in ("", None)]
        cost = sum(float(r["cost_usd"] or 0) for r in rs)
        b.append(f"| {sc} | {pct(ok, n)} | {pct(sum(r['status'] == 'rate_limited' for r in rs), n)} | "
                 f"{pct(sum(r['status'] == 'timeout' for r in rs), n)} | {sum(int(r['retries'] or 0) for r in rs) / n:.2f} | "
                 f"{max(q) if q else 'not measured'} | {tok} | {'$%.2f' % (1000 * cost / ok) if ok else '–'} |")
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


def main():
    rows = list(csv.DictReader((ROOT / "results" / "results.csv").open()))
    a, b = table2(rows)
    md = ["## Table 2a: Latency", *a, "", "## Table 2b: Outcomes", *b, "", "## Table 3: Retrieval settings (real, 17 answer questions)", *table3(rows),
          "", "Notes: stand-in times are simulated (time compressed ×20, converted back). Real quality-run times include TPM pacing "
              "waits (runs were back-to-back); gate refusals make no model call and are not timed. Refused-wrongly % = 100 − correct − "
              "wrong − correctly refused. Percentiles are nearest-rank; with n < 100, p99 is the slowest request."]
    text = "\n".join(md)
    (ROOT / "results" / "tables.md").write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
