"""Phase 7: loadgen, build_results, make_tables (counts, percentages, served vs full-answer), steady-run script."""
import asyncio
import json
import random
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "exp"), str(ROOT / "app"), str(ROOT / "standin")]
import build_results as br  # noqa: E402
import loadgen  # noqa: E402
import make_tables as mt  # noqa: E402
import run_steady_real as sr  # noqa: E402

# ---- loadgen ----

def test_arrivals_are_seeded_open_loop_and_include_bursts():
    cfg = {"segments": [(0, 60, 2.0)], "bursts": [(5.0, 40)]}
    a, b = loadgen.arrivals(cfg, random.Random(1)), loadgen.arrivals(cfg, random.Random(1))
    assert a == b and a == sorted(a)
    burst = [t for t in a if 300 <= t < 360]
    assert len(burst) >= 40                                      # 40 burst arrivals inside minute 5
    assert 90 <= len(a) - 40 <= 150                              # ~120 Poisson arrivals at 2/min over 60 min


def test_question_mix_is_zipf_like():
    order, w = loadgen.question_mix(["A01", "A02", "A03", "A04"], random.Random(0))
    assert sorted(order) == ["A01", "A02", "A03", "A04"] and w == [1, 1 / 2, 1 / 3, 1 / 4]


def test_grade_of_carries_real_grade_only_for_unchanged_released_answers():
    real = {"A01": "correct"}
    assert loadgen.grade_of("A01", "answered", True, real, "answer") == "correct"
    assert loadgen.grade_of("A01", "answered", False, real, "answer") == ""
    assert loadgen.grade_of("A01", "refused", False, real, "answer") == "refused_wrongly"
    assert loadgen.grade_of("A08", None, False, real, "refuse") == "refused_correctly"


def test_extractive_quotes_two_passages_with_citations():
    c = {"paper": "moshi", "section_heading": "4.2 Audio data", "page_start": 17, "page_end": 18, "text": "word " * 100}
    out = loadgen.extractive([c, c, c])
    assert out.count("[Moshi, §4.2 Audio data, pp.17–18]") == 2 and "capacity limit" in out


# ---- build_results ----

def test_results_columns_include_every_mandatory_column():
    mandatory = ["request_id", "scenario", "source", "start_time", "first_token_ms", "total_ms", "status", "retries",
                 "tokens_in", "tokens_out", "retrieval_setting", "answer_grade"]
    assert br.COLS[:12] == mandatory


def test_status_and_cost():
    assert [br.status_of(s) for s in (200, 429, None, 500)] == ["ok", "rate_limited", "timeout", "error"]
    calls = [{"model": "openai/gpt-oss-120b", "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}}]
    assert br.cost(calls) == pytest.approx(0.75)                  # $0.15 in + $0.60 out per 1M


@pytest.mark.skipif(not (ROOT / "results" / "probe_log.jsonl").exists(), reason="logs missing")
def test_real_rows_from_logs():
    probe = list(br.probe_rows())
    smoke = list(br.smoke_rows())
    assert len(probe) == 32 and all(r["status"] == "ok" for r in probe)
    assert len(smoke) == 7 and [r["status"] for r in smoke].count("rate_limited") == 1   # run 1 stopped on the 429


# ---- make_tables: counts, percentages, served vs full answer ----

def r(status, served="", **kw):
    return {"status": status, "served": served, **kw}


def test_served_vs_full_answer_split():
    rows = [r("ok", "answer"), r("ok", "refusal"), r("rate_limited", "cache"), r("rate_limited", "extractive"),
            r("rate_limited", "refusal"), r("timeout", "given up"), r("error", "refusal"), r("ok")]
    v = mt.views(rows)
    assert v == {"n": 8, "served": 5, "full": 4, "rate_limited": 3, "rate_limited_no_full": 2, "timeout": 1, "error": 1}


def test_pct_and_nearest_rank_percentiles():
    assert mt.pct(1, 3) == "33.3" and mt.pct(0, 0) == "–"
    xs = list(range(1, 21))
    assert mt.pctl(xs, 50) == 10 and mt.pctl(xs, 95) == 19 and mt.pctl(xs, 99) == 20 and mt.pctl([None], 50) is None


# ---- steady-run script ----

def test_steady_plan_is_16_seeded_requests_at_about_1_5_per_min():
    qs = {f"A{i:02d}": {} for i in range(1, 18)}
    p1, p2 = sr.plan(qs), sr.plan(qs)
    assert p1 == p2 and len(p1) == 16 and [x[0] for x in p1] == [f"steady-{i:02d}" for i in range(16)]
    assert sr.arrival_times()[-1] > 60 * 5                        # 16 arrivals at 1.5/min span several minutes


def test_finished_excludes_budget_and_stopped(tmp_path):
    log = tmp_path / "l.jsonl"
    rows = [{"type": "request", "request_id": "steady-00", "outcome_reason": "judge: supported", "status": "ok"},
            {"type": "request", "request_id": "steady-01", "outcome_reason": "budget", "status": "budget"},
            {"type": "request", "request_id": "steady-02", "outcome_reason": "stopped: HTTP 429", "status": "rate_limited"},
            {"type": "request", "request_id": "steady-03", "outcome_reason": "user gave up", "status": "timeout"}]
    log.write_text("".join(json.dumps(x) + "\n" for x in rows))
    assert sr.finished(log) == {"steady-00", "steady-03"}


def test_steady_mini_run_against_standin_stops_on_429_and_never_logs_key(tmp_path):
    import server as standin
    import pipeline as pl
    fake_key = "gsk_FAKE_steady_key_789"

    async def go():
        s = standin.StandIn(speed=200, latency={}, replay={}, tpd_remaining=0)   # every call → 429 per day
        srv = await s.serve(port=0)
        url = f"http://127.0.0.1:{srv.sockets[0].getsockname()[1]}/openai/v1/chat/completions"
        chunk = {"chunk_id": "c1", "paper": "attention", "page_start": 7, "page_end": 7, "section_heading": "5.3 Optimizer", "text": "x"}
        prepared = {"A02": [{"type": "request", "attempts": [], "retrieved": [{"chunk_id": "c1", "label": "S1"}], "gate_score": 0.8}, [chunk]]}
        qs = {"A02": {"question": "How many warmup steps?", "expected": "answer", "must_include": ["4000"], "paper": "attention",
                      "evidence_page": 7, "evidence_phrase": "warmup"}}
        try:
            async with httpx.AsyncClient() as client:
                run = sr.Run(client, fake_key, url, tmp_path / "log.jsonl", 50_000, 200, qs, prepared, {"c1": chunk})
                # 60 simulated s apart (real runs: ~40 s apart, ~1.5 s each) — a call already in flight when a 429
                # arrives cannot be un-sent; the guarantee is that no call STARTS after the stop
                out = await run.run([("steady-00", 0.0, "A02"), ("steady-01", 60.0, "A02")])
                return run, out
        finally:
            srv.close()
            await srv.wait_closed()

    run, out = asyncio.run(go())
    assert run.stop and "HTTP 429" in run.stop
    text = (tmp_path / "log.jsonl").read_text()
    assert fake_key not in text
    reqs = [json.loads(x) for x in text.splitlines() if json.loads(x)["type"] == "request"]
    assert reqs[0]["status"] == "rate_limited" and sr.finished(tmp_path / "log.jsonl") == set()   # resumable
    calls = [json.loads(x) for x in text.splitlines() if json.loads(x)["type"] == "call"]
    assert len(calls) == 1                                                       # no retry, second request never called
