"""Phase 7: open-loop load against the stand-in (never Groq). One stand-in process per scenario.

  python exp/loadgen.py [scenario ...]   → results/load_runs.jsonl (one row per request; scenarios replaced on re-run)

Per request: precomputed retrieval (default setting: hybrid, 200, top-8; deterministic per question), gate, then the
shared pipeline core (answer_steps) under asyncio with the Limiter and the retry policy. A simulated user gives up after
PATIENCE_S (→ status timeout, request cancelled, reservations released). When a call is shed (capacity cannot arrive
before the deadline) or hits a per-day 429, the fallback ladder serves: validated-answer cache (this run) → extractive
(top-2 passages with citations, 0 tokens) → honest refusal. Time is compressed ×SPEED; all reported times are simulated.
"""
import asyncio
import copy
import json
import math
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "exp")]
import aclient  # noqa: E402
import limiter as lim  # noqa: E402
import pipeline as pl  # noqa: E402
import prompts  # noqa: E402

OUT = ROOT / "results" / "load_runs.jsonl"
SPEED = 20.0
PATIENCE_S = 20.0          # assumption: a person waits ~20 s for a cited answer (brief: "they give up sooner than you expect")
SEED = 7
PRICES = {pl.GEN_MODEL: (0.15, 0.60), pl.JUDGE_MODEL: (0.075, 0.30)}   # $ per 1M input/output tokens (Groq models page, 2026-11-14)
RETRIEVAL = "200 tok, 8 pieces, hybrid RRF, no rerank"

# arrivals: list of (start_min, end_min, rate_per_min) segments + optional (at_min, n) bursts; stand-in flags
SCENARIOS = {
    "steady":          {"segments": [(0, 30, 2.0)], "standin": []},
    "burst":           {"segments": [(0, 20, 1.0)], "bursts": [(5.0, 40)], "standin": []},
    "overload":        {"segments": [(0, 15, 8.0)], "standin": []},
    "tpm_halved":      {"segments": [(0, 30, 2.0)], "standin": ["--tpm", "4000"]},
    "daily_exhausted": {"segments": [(0, 15, 2.0)], "standin": ["--tpd-remaining", "6000"]},
}


def arrivals(cfg, rng):
    """Open-loop Poisson arrival times in simulated seconds."""
    times = []
    for start, end, rate in cfg["segments"]:
        t = start * 60
        while True:
            t += rng.expovariate(rate / 60)
            if t >= end * 60:
                break
            times.append(t)
    for at, n in cfg.get("bursts", []):
        times += [at * 60 + rng.uniform(0, 60) for _ in range(n)]
    return sorted(times)


def question_mix(questions, rng):
    """Zipf-like popularity: after an incident, a few questions are asked by many people."""
    order = list(questions)
    rng.shuffle(order)
    weights = [1 / (k + 1) for k in range(len(order))]
    return order, weights


def grade_of(qid, outcome, released_same, real_grades, expected):
    if outcome == "answered" and released_same:
        return real_grades[qid]
    if outcome == "answered":
        return ""                                             # not the real released text: ungraded
    return "refused_wrongly" if expected == "answer" else "refused_correctly"


def extractive(chunks):
    """Zero-token fallback: the top-2 retrieved passages, quoted, with rendered citations."""
    parts = []
    for c in chunks[:2]:
        snippet = " ".join(c["text"].split()[:40])
        parts.append(f"“{snippet}…” [{prompts.PAPER_NAMES[c['paper']]}, §{c['section_heading']}, {prompts.pages(c)}]")
    return ("I can't generate a checked answer right now (capacity limit). The most relevant passages are:\n" + "\n".join(parts))


async def run_scenario(name, cfg, url, prepared, questions, real, rng):
    clock = lim.SimClock(SPEED)
    limiter, budget = lim.Limiter(clock=clock), aclient.RetryBudget()
    order, weights = question_mix(sorted(prepared), rng)
    cache, rows = {}, []
    wall0, sim0 = datetime.now(timezone.utc), clock.now()

    async def one(i, at, qid):
        await clock.sleep(at - (clock.now() - sim0))
        start = clock.now()
        q = questions[qid]
        res, chunks = copy.deepcopy(prepared[qid])
        row = {"request_id": f"{name}-{i:04d}", "scenario": name, "source": "stand-in", "qid": qid,
               "start_time": (wall0 + timedelta(seconds=start - sim0)).isoformat(), "retries": 0, "tokens_in": 0,
               "tokens_out": 0, "cost_usd": 0.0, "retrieval_setting": RETRIEVAL, "queue_len_at_start": limiter.waiters(),
               "served": "", "outcome_reason": "", "first_token_ms": None, "total_ms": None}
        deadline = start + PATIENCE_S
        rows_seen = []

        async def acall(step, model, messages, max_tokens):
            prio = 1 if step == "generate#1" else 0           # in-progress work before new work
            r, n = await aclient.call(client, "stand-in-key", url, limiter, budget, model, messages, max_tokens,
                                      pl.worst_case(messages, max_tokens), deadline=deadline, priority=prio, rng=rng)
            row["retries"] += n
            u = r.get("usage") or {}
            row["tokens_in"] += u.get("prompt_tokens", 0)
            row["tokens_out"] += u.get("completion_tokens", 0)
            pin, pout = PRICES[model]
            row["cost_usd"] += (u.get("prompt_tokens", 0) * pin + u.get("completion_tokens", 0) * pout) / 1e6
            rows_seen.append(r)
            return r

        def finish(status, served, reason, outcome=None, released_same=False):
            t = (clock.now() - start) * 1000
            row.update(status=status, served=served, outcome_reason=reason, first_token_ms=round(t, 1), total_ms=round(t, 1),
                       answer_grade=grade_of(qid, outcome, released_same, real, q["expected"]))

        def fallback(reason):
            if qid in cache:
                return finish("rate_limited", "cache", reason, "answered", released_same=True)
            if chunks:
                return finish("rate_limited", "extractive", reason, "extractive")
            return finish("rate_limited", "refusal", reason)

        try:
            if chunks is None:
                return finish("ok", "refusal", "gate", "refused")
            outcome, reason, answer = await asyncio.wait_for(
                pl.adrive(pl.answer_steps(q["question"], chunks, res), acall), timeout=PATIENCE_S / SPEED)
            last = rows_seen[-1] if rows_seen else {}
            if outcome == "error" and last.get("http_status") == 429:
                return fallback("daily limit" if aclient.is_daily(last) else "per-minute limit after retries")
            if outcome == "error":
                return finish("error", "refusal", reason)
            if outcome == "answered":
                cache[qid] = answer
            finish("ok", "answer" if outcome == "answered" else "refusal", reason, outcome,
                   released_same=outcome == "answered")
        except lim.Shed as e:
            fallback(f"shed: {e}")
        except asyncio.TimeoutError:
            row.update(status="timeout", served="given up", outcome_reason=f"user gave up after {PATIENCE_S:.0f} s",
                       first_token_ms=None, total_ms=round((clock.now() - start) * 1000, 1),
                       answer_grade=grade_of(qid, None, False, real, q["expected"]))
        finally:
            rows.append(row)

    times = arrivals(cfg, rng)
    async with httpx.AsyncClient(limits=httpx.Limits(max_connections=500)) as client:
        await asyncio.gather(*(one(i, t, rng.choices(order, weights)[0]) for i, t in enumerate(times)))
    return rows, limiter.max_waiters


def start_standin(extra):
    port = 18000 + random.randint(0, 999)
    proc = subprocess.Popen([sys.executable, str(ROOT / "standin" / "server.py"), "--port", str(port), "--speed", str(SPEED),
                             "--seed", str(SEED)] + extra, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    line = proc.stdout.readline()
    if "stand-in on" not in line:
        proc.kill()
        raise RuntimeError(f"stand-in failed to start: {line}")
    return proc, f"http://127.0.0.1:{port}/openai/v1/chat/completions"


def prepare_all(questions):
    retriever = pl.Retriever(200, "hybrid", 8)
    p = pl.Pipeline(retriever, None, "unused")
    return {qid: p.prepare(q["question"], qid) for qid, q in questions.items()}


def main(names):
    questions = {q["id"]: q for q in json.loads((ROOT / "eval" / "questions.json").read_text())["answer_questions"]}
    real = {r["qid"]: r["final_grade"] for r in __import__("csv").DictReader((ROOT / "results" / "answers.csv").open()) if r["setting"] == "default"}
    prepared = prepare_all(questions)
    names = names or list(SCENARIOS)
    old = [json.loads(x) for x in OUT.read_text().splitlines()] if OUT.exists() else []
    keep = [r for r in old if r["scenario"] not in names]
    out = []
    for name in names:
        proc, url = start_standin(SCENARIOS[name]["standin"])
        try:
            t0 = time.time()
            rows, peak = asyncio.run(run_scenario(name, SCENARIOS[name], url, prepared, questions, real, random.Random(SEED)))
        finally:
            proc.terminate()
            proc.wait(timeout=10)
        st = {s: sum(r["status"] == s for r in rows) for s in ("ok", "rate_limited", "timeout", "error")}
        print(f"{name:<16} {len(rows):>4} requests in {time.time() - t0:5.1f} s wall | {st} | peak queue {peak} | "
              f"served {dict(__import__('collections').Counter(r['served'] for r in rows))}", flush=True)
        out += rows
    OUT.write_text("".join(json.dumps(r) + "\n" for r in keep + out))


if __name__ == "__main__":
    main(sys.argv[1:])
