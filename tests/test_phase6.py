"""Phase 6: stand-in service + async limiter + retry policy. No Groq: everything runs against the in-process stand-in."""
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "standin")]
import aclient  # noqa: E402
import limiter as lim  # noqa: E402
import llm  # noqa: E402
import pipeline as pl  # noqa: E402
import server as standin  # noqa: E402

SPEED = 100.0
REPLAY = {"How many warmup steps?": {"gen": ["It used 4000 warmup steps [S1]."], "completion": [20],
                                     "judge": {"It used 4000 warmup steps [S1].": json.dumps({"verdict": "supported", "claim": "", "reason": "ok"})}}}
LAT = {(m, b): [(500.0, 600.0)] for m in (pl.GEN_MODEL, pl.JUDGE_MODEL) for b in (0, 1, 2)}


def run(coro):
    return asyncio.run(coro)


async def with_standin(fn, **kw):
    s = standin.StandIn(speed=SPEED, latency=LAT, replay=REPLAY, **kw)
    srv = await s.serve(port=0)
    url = f"http://127.0.0.1:{srv.sockets[0].getsockname()[1]}/openai/v1/chat/completions"
    try:
        async with httpx.AsyncClient() as client:
            return await fn(s, client, url)
    finally:
        srv.close()
        await srv.wait_closed()


def gen_msgs(q="How many warmup steps?"):
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": f"Sources:\n\nx\n\nQuestion: {q}"}]


# ---- stand-in ----

def test_standin_streams_replayed_answer_with_usage_and_headers():
    async def t(s, client, url):
        row = await llm.achat(client, "k", pl.GEN_MODEL, gen_msgs(), 350, url=url)
        assert row["http_status"] == 200 and row["text"] == "It used 4000 warmup steps [S1]."
        assert row["usage"]["completion_tokens"] == 20 and row["finish_reason"] == "stop"
        assert row["first_content_ms"] < row["total_ms"]
        assert aclient.header(row, "x-ratelimit-limit-tokens") == "8000"
        unknown = await llm.achat(client, "k", pl.GEN_MODEL, gen_msgs("unknown question?"), 350, url=url)
        assert unknown["text"] == "NOT_IN_PAPERS"
    run(with_standin(t))


def test_standin_429_shape_like_real_and_bucket_refills():
    async def t(s, client, url):
        m = s.models[pl.GEN_MODEL]
        m.level, m.at = 500.0, s.now()                                              # bucket nearly drained
        row = await llm.achat(client, "k", pl.GEN_MODEL, gen_msgs(), 1000, url=url)   # prompt + 1000 > 500 left
        assert row["http_status"] == 429
        err = json.loads(row["error_body"])["error"]
        assert err["type"] == "tokens" and err["code"] == "rate_limit_exceeded"
        assert "tokens per minute (TPM): Limit 2000" in err["message"] and "org_<redacted>" in err["message"]
        assert int(aclient.header(row, "retry-after")) >= 1
        for h in ("x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens", "x-ratelimit-remaining-requests"):
            assert aclient.header(row, h) is not None
        ok = await llm.achat(client, "k", pl.GEN_MODEL, gen_msgs(), 300, url=url)   # fits what is left
        assert ok["http_status"] == 200
        level_after = s.models[pl.GEN_MODEL].level
        await asyncio.sleep(3 / SPEED)                                              # 3 simulated seconds
        s.models[pl.GEN_MODEL].refill(s.now())
        assert s.models[pl.GEN_MODEL].level > level_after                          # continuous refill
    run(with_standin(t, tpm=2000))


def test_standin_daily_limit_and_injected_5xx():
    async def daily(s, client, url):
        row = await llm.achat(client, "k", pl.GEN_MODEL, gen_msgs(), 100, url=url)
        assert row["http_status"] == 429 and "tokens per day (TPD)" in row["error_body"] and aclient.is_daily(row)
    run(with_standin(daily, tpd_remaining=0))

    async def fail(s, client, url):
        row = await llm.achat(client, "k", pl.GEN_MODEL, gen_msgs(), 100, url=url)
        assert row["http_status"] == 500
    run(with_standin(fail, p5xx=1.0))


def test_standin_slower_under_load():
    s = standin.StandIn(speed=SPEED, latency=LAT, replay=REPLAY, alpha=0.5)
    assert s.sample_latency(pl.GEN_MODEL, 2000, in_flight=5)[1] == pytest.approx(0.6 * (1 + 0.5 * 4))


# ---- limiter ----

def test_limiter_never_admits_beyond_bucket():
    async def t():
        L = lim.Limiter(clock=lim.SimClock(SPEED))
        levels = []

        async def one(i):
            r = await L.acquire("m", 3000)
            levels.append(L.level["m"])
            await L.clock.sleep(0.5)
            L.settle(r, 2500)
        await asyncio.gather(*(one(i) for i in range(12)))
        assert min(levels) >= 0                                      # bucket never negative at admission
        assert L.outstanding["m"] == 0 and L.waiters() == 0
    run(t())


def test_limiter_priority_in_progress_first():
    async def t():
        L = lim.Limiter(clock=lim.SimClock(SPEED))
        L.level["m"], L.at["m"], L.limit["m"] = 0, L.clock.now(), 8000    # empty bucket: everyone waits
        order = []

        async def req(name, prio, delay):
            await asyncio.sleep(delay / SPEED)
            r = await L.acquire("m", 1000, priority=prio)
            order.append(name)
            L.settle(r, 1000)
        await asyncio.gather(req("new-1", 1, 0), req("new-2", 1, 0.01), req("judge", 0, 0.02))
        assert order[0] == "new-1" or order.index("judge") < order.index("new-2")
        assert order.index("judge") < order.index("new-2")
    run(t())


def test_limiter_sheds_when_wait_passes_deadline():
    async def t():
        L = lim.Limiter(clock=lim.SimClock(SPEED))
        L.level["m"], L.at["m"], L.limit["m"] = 0, L.clock.now(), 8000
        with pytest.raises(lim.Shed):
            await L.acquire("m", 4000, deadline=L.clock.now() + 5)          # needs ~30 s of refill
        assert L.waiters() == 0 and L.outstanding.get("m", 0) == 0
    run(t())


def test_cancellation_never_leaks_reservations():
    async def t():
        L = lim.Limiter(clock=lim.SimClock(SPEED))
        L.level["m"], L.at["m"], L.limit["m"] = 0, L.clock.now(), 8000
        waiting = asyncio.create_task(L.acquire("m", 5000))                  # (a) cancelled while queued
        await asyncio.sleep(0.01)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert L.waiters() == 0 and L.outstanding.get("m", 0) == 0
        L.level["m"], L.at["m"] = 8000, L.clock.now()
        r1 = await L.acquire("m", 3000)                                      # (b) admitted, never sent → full refund
        L.cancel(r1)
        assert L.outstanding["m"] == 0 and L._refill("m") == pytest.approx(8000, abs=1)
        r2 = await L.acquire("m", 3000)                                      # (c) sent → keeps only the prompt estimate
        r2.sent = True
        L.cancel(r2, prompt_estimate=1000)
        assert L.outstanding["m"] == 0 and L._refill("m") == pytest.approx(7000, abs=5)
        L.cancel(r2, prompt_estimate=1000)                                   # double release is a no-op
        assert L.outstanding["m"] == 0
    run(t())


def test_cancel_inflight_call_through_aclient_releases():
    async def t(s, client, url):
        L = lim.Limiter(clock=lim.SimClock(SPEED))
        task = asyncio.create_task(aclient.call(client, "k", url, L, aclient.RetryBudget(), pl.GEN_MODEL, gen_msgs(), 350, 1000))
        await asyncio.sleep(0.2 / SPEED)                                     # admitted and in flight
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert L.outstanding[pl.GEN_MODEL] == 0
    run(with_standin(t))


def test_settle_refunds_unused_and_syncs_down_from_headers():
    async def t():
        L = lim.Limiter(clock=lim.SimClock(SPEED))
        r = await L.acquire("m", 3000)
        L.settle(r, 1000, [["x-ratelimit-limit-tokens", "4000"], ["x-ratelimit-remaining-tokens", "2500"]])
        assert L.limit["m"] == 4000 and L._refill("m") <= 2500 + 5
    run(t())


# ---- retries ----

def test_retry_honours_retry_after_and_reenters_limiter():
    async def t(s, client, url):
        L = lim.Limiter(clock=lim.SimClock(SPEED))                           # believes 8000; stand-in has 2000
        m = s.models[pl.GEN_MODEL]
        m.level, m.at = 0.0, s.now()                                         # provider bucket drained by others
        row, retries = await aclient.call(client, "k", url, L, aclient.RetryBudget(), pl.GEN_MODEL, gen_msgs(), 1500, 1700)
        assert row["http_status"] == 200 and retries == 1
        assert L.limit[pl.GEN_MODEL] == 2000                                 # learned from the 429's headers
        assert L.outstanding[pl.GEN_MODEL] == 0
    run(with_standin(t, tpm=2000))


def test_daily_429_is_not_retried():
    async def t(s, client, url):
        L = lim.Limiter(clock=lim.SimClock(SPEED))
        row, retries = await aclient.call(client, "k", url, L, aclient.RetryBudget(), pl.GEN_MODEL, gen_msgs(), 100, 200)
        assert row["http_status"] == 429 and retries == 0
    run(with_standin(t, tpd_remaining=0))


def test_retry_budget_caps_retries():
    b = aclient.RetryBudget(ratio=0.1, burst=1)
    b.calls = 10
    assert b.allow()
    b.retries = 2
    assert not b.allow()


# ---- the shared pipeline core under asyncio, against the stand-in ----

def test_answer_steps_async_end_to_end():
    async def t(s, client, url):
        L, budget = lim.Limiter(clock=lim.SimClock(SPEED)), aclient.RetryBudget()
        chunk = {"chunk_id": "attention-200-0001", "paper": "attention", "page_start": 7, "page_end": 7,
                 "section_heading": "5.3 Optimizer", "text": "warmup_steps = 4000"}
        res = {"attempts": []}

        async def acall(step, model, messages, max_tokens):
            row, _ = await aclient.call(client, "k", url, L, budget, model, messages, max_tokens, pl.worst_case(messages, max_tokens))
            return row
        # replay is keyed by question; the judge prompt must quote the exact replayed answer
        s.replay["How many warmup steps?"]["judge"]["It used 4000 warmup steps [S1]."] = json.dumps({"verdict": "supported", "claim": "", "reason": "ok"})
        outcome = await pl.adrive(pl.answer_steps("How many warmup steps?", [chunk], res), acall)
        assert outcome[0] == "answered" and "[Attention Is All You Need, §5.3 Optimizer, p.7]" in outcome[2]
    run(with_standin(t))
