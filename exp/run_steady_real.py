"""Phase 7/8: the steady load scenario, 16 requests at 1.5/min, against real Groq (--go-real) or the stand-in (--standin).

  python exp/run_steady_real.py --go-real                                   # real: key from .env
  python exp/run_steady_real.py --standin URL --speed 20 --log PATH [--cap N] [--stop-after K]   # zero-token dry run

Same pipeline as everywhere (default setting: hybrid 200 top-8, gate 0.60, generate → check → judge → ≤1 correction),
async with the Limiter. NO retries: 401/403/429 stop the whole run. Guard: before every call, tokens already used today
(from THIS log) + the call's worst case must be ≤ cap per model (default 50K, the real-load share), else the request is
refused for budget and the run stops. Resume: request_ids with a finished request row are skipped. A simulated user gives
up after PATIENCE_S (status timeout). The key never reaches the log or stdout; org IDs are redacted. Grades are the
automatic first pass (exp/grade.py), marked grade_source=auto.
"""
import argparse
import asyncio
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "exp")]
import limiter as lim  # noqa: E402
import llm  # noqa: E402
import loadgen  # noqa: E402
import pipeline as pl  # noqa: E402
from grade import grade  # noqa: E402

N_REQUESTS, RATE_PER_MIN, SEED, CAP = 16, 1.5, 11, 50_000
REAL_LOG = ROOT / "results" / "steady_real_log.jsonl"


def arrival_times(n=N_REQUESTS, rate=RATE_PER_MIN, seed=SEED):
    """Exactly n Poisson arrivals (simulated seconds from start) at `rate` per minute; seeded, so resume replays them."""
    rng, t, out = random.Random(seed), 0.0, []
    for _ in range(n):
        t += rng.expovariate(rate / 60)
        out.append(t)
    return out


def plan(questions, n=N_REQUESTS, seed=SEED):
    """[(request_id, arrival_s, qid)] — same Zipf popularity as the stand-in load runs."""
    rng = random.Random(seed + 1)
    order, weights = loadgen.question_mix(sorted(questions), rng)
    return [(f"steady-{i:02d}", t, rng.choices(order, weights)[0]) for i, t in enumerate(arrival_times(n, seed=seed))]


def finished(log_path):
    if not log_path.exists():
        return set()
    return {r["request_id"] for r in map(json.loads, log_path.read_text().splitlines())
            if r["type"] == "request" and r["outcome_reason"] != "budget" and r["status"] in ("ok", "timeout")}


class Run:
    def __init__(self, client, key, url, log_path, cap, speed, questions, prepared, chunks_by_id):
        self.client, self.key, self.url, self.log_path, self.speed = client, key, url, log_path, speed
        self.questions, self.prepared, self.chunks_by_id = questions, prepared, chunks_by_id
        self.clock = lim.SimClock(speed)
        self.limiter = lim.Limiter(clock=self.clock)
        day = datetime.now(timezone.utc).date().isoformat()
        self.budget = pl.Budget(token_cap=cap, call_cap=10_000, used=pl.prior_usage(log_path, day))
        self.stop = None          # reason string once the run must stop
        self.wall0, self.sim0 = datetime.now(timezone.utc), self.clock.now()   # start_time in SIMULATED time (dry runs ×speed)

    def log(self, row):
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False).replace(self.key, "***")
        with self.log_path.open("a") as f:
            f.write(re.sub(r"\borg_[A-Za-z0-9]+", "org_<redacted>", line) + "\n")

    async def call(self, rid, step, model, messages, max_tokens, deadline, priority):
        worst = pl.worst_case(messages, max_tokens)
        if self.stop or not self.budget.allows(model, worst):
            self.log({"type": "budget_refused", "request_id": rid, "step": step, "model": model, "worst_case": worst,
                      "used": self.budget.used.get(model, 0), "cap": self.budget.cap(model)})
            self.stop = self.stop or f"budget guard: {model} used {self.budget.used.get(model, 0)} + worst {worst} > {self.budget.cap(model)}"
            return None
        res = await self.limiter.acquire(model, worst + 100, deadline=deadline, priority=priority)
        res.sent = True
        try:
            row = await llm.achat(self.client, self.key, model, messages, max_tokens, url=self.url)
        except asyncio.CancelledError:
            self.limiter.cancel(res, prompt_estimate=worst - max_tokens)
            raise
        total = (row.get("usage") or {}).get("total_tokens")
        self.limiter.settle(res, total if row["http_status"] == 200 else 0, row.get("response_headers"))
        self.budget.charge(model, total if total is not None else (0 if row["http_status"] == 429 else worst))
        for k in ("total_ms", "first_content_ms", "first_chunk_ms"):       # simulated ms (×speed; speed=1 for real)
            if row.get(k) is not None:
                row[k] = round(row[k] * self.speed, 1)
        self.log({"type": "call", "request_id": rid, "step": step, "ts": datetime.now(timezone.utc).isoformat(),
                  "worst_case": worst, **row})
        if row["http_status"] in llm.STOP_STATUSES:
            self.stop = f"HTTP {row['http_status']} at {rid} {step}"
            raise llm.Stopped(self.stop)
        return row

    async def one(self, rid, at, qid, t0):
        await self.clock.sleep(at - (self.clock.now() - t0))
        if self.stop:
            return
        q = self.questions[qid]
        res, chunks = json.loads(json.dumps(self.prepared[qid]))   # deep copy
        res.update(request_id=rid, qid=qid, start_time=(self.wall0 + timedelta(seconds=self.clock.now() - self.sim0)).isoformat())
        start, deadline = self.clock.now(), self.clock.now() + loadgen.PATIENCE_S
        status, served = "ok", "refusal"
        try:
            if chunks is None:
                outcome, reason, answer = "refused", "gate", None
            else:
                async def acall(step, model, messages, max_tokens):
                    return await self.call(rid, step, model, messages, max_tokens, deadline, 1 if step == "generate#1" else 0)
                outcome, reason, answer = await asyncio.wait_for(pl.adrive(pl.answer_steps(q["question"], chunks, res), acall),
                                                                 timeout=loadgen.PATIENCE_S / self.speed)
            served = "answer" if outcome == "answered" else "refusal"
            if reason == "budget":
                status = "budget"
            elif outcome == "error":
                status = "error"
        except asyncio.TimeoutError:
            outcome, reason, answer, status, served = None, f"user gave up after {loadgen.PATIENCE_S:.0f} s", None, "timeout", "given up"
        except lim.Shed as e:
            outcome, reason, answer, status, served = None, f"shed: {e}", None, "rate_limited", "refusal"
        except llm.Stopped as e:
            outcome, reason, answer, status, served = None, f"stopped: {e}", None, "rate_limited", "refusal"
        t = round((self.clock.now() - start) * 1000, 1)
        res.update(outcome=outcome or "none", outcome_reason=reason, final_answer=answer, status=status, served=served,
                   first_token_ms=t if status != "timeout" else None, total_ms=t)
        g = grade(q, {**res, "outcome": res["outcome"] if outcome else "refused"}, self.chunks_by_id) if status != "budget" else {}
        res.update(answer_grade=g.get("auto_grade", ""), grade_source="auto",
                   gold_evidence_in_context=g.get("evidence_in_context"))
        self.log(res)
        return res

    async def run(self, todo):
        t0 = self.clock.now()
        results = await asyncio.gather(*(self.one(rid, at, qid, t0) for rid, at, qid in todo), return_exceptions=True)
        return [r for r in results if isinstance(r, dict)]


async def amain(target_url, key, log_path, cap, speed, stop_after):
    questions = {q["id"]: q for q in json.loads((ROOT / "eval" / "questions.json").read_text())["answer_questions"]}
    chunks_by_id = {c["chunk_id"]: c for c in map(json.loads, (ROOT / "data" / "index" / "chunks.jsonl").read_text().splitlines())}
    prepared = loadgen.prepare_all(questions)
    done = finished(log_path)
    todo = [p for p in plan(questions) if p[0] not in done]
    if stop_after is not None:                   # dry-run only: simulate an interrupted run, to prove resume
        todo = todo[:stop_after]
    async with httpx.AsyncClient() as client:
        run = Run(client, key, target_url, log_path, cap, speed, questions, prepared, chunks_by_id)
        print(f"{len(done)} finished in {log_path.name}, {len(todo)} to run; guard {cap} per model; used today {run.budget.used}")
        out = await run.run(todo)
        print(f"ran {len(out)} | stop: {run.stop or 'none'} | used today {run.budget.used}")
        return out, run


def main(argv):
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--go-real", action="store_true")
    g.add_argument("--standin")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--log")
    ap.add_argument("--cap", type=int, default=CAP)
    ap.add_argument("--stop-after", type=int)
    a = ap.parse_args(argv)
    if a.go_real:
        if a.speed != 1.0 or a.stop_after is not None or a.log:
            sys.exit("--go-real runs in real time with the default log; --speed/--stop-after/--log are dry-run only")
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        key = os.environ.get("GROQ_API_KEY")
        if not key:
            sys.exit("GROQ_API_KEY not set")
        url, log_path = llm.GROQ_URL, REAL_LOG
    else:
        if not a.log:
            sys.exit("--standin needs --log (never write dry runs into the real log)")
        key, url, log_path = "stand-in-key", a.standin, Path(a.log)
    try:
        asyncio.run(amain(url, key, log_path, a.cap, a.speed, a.stop_after))
    except Exception as e:  # never print the key
        sys.exit(f"crashed: {type(e).__name__}: {str(e).replace(key, '***')}")


if __name__ == "__main__":
    main(sys.argv[1:])
