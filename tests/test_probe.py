import itertools
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "exp"))
import probe  # noqa: E402

FAKE_KEY = "gsk_FAKE_SECRET_do_not_leak_123"


def sse(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


OK_STREAM = sse(
    {"choices": [{"delta": {"reasoning": "think"}}]},
    {"choices": [{"delta": {"content": "Hello"}}]},
    {"choices": [{"delta": {"content": " world"}, "finish_reason": "stop"}]},
    {"choices": [], "usage": {"prompt_tokens": 50, "completion_tokens": 40, "total_tokens": 90,
                              "completion_tokens_details": {"reasoning_tokens": 12}}},
)


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok_handler(seen):
    def handler(request):
        seen.append(json.loads(request.content)["model"])
        return httpx.Response(200, text=OK_STREAM, headers={"x-ratelimit-remaining-tokens": "7900"})
    return handler


@pytest.fixture
def log(tmp_path):
    return tmp_path / "probe_log.jsonl"


def rows_of(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_stream_parser_ttft_and_usage(monkeypatch, log):
    # first call: t0, reasoning, content, content, end; later calls (pilot has 3) see a frozen clock
    ticks = itertools.chain([0.0, 0.10, 0.25, 0.30, 0.40], itertools.repeat(1.0))
    monkeypatch.setattr(probe, "now", lambda: next(ticks))
    probe.run("pilot", make_client(ok_handler([])), FAKE_KEY, log, sleep=lambda s: None, out=lambda m: None)
    r = rows_of(log)[0]
    assert r["first_chunk_ms"] == 100.0        # reasoning chunk counts as first chunk
    assert r["first_content_ms"] == 250.0      # but not as first content
    assert (r["prompt_tokens"], r["completion_tokens"], r["reasoning_tokens"]) == (50, 40, 12)
    assert r["finish_reason"] == "stop" and r["content_head"] == "Hello world" and r["tokens_charged"] == 90


def test_full_plan_shape():
    full, pilot = probe.plan("full"), probe.plan("pilot")
    assert len(pilot) == 3 and len(full) == 27
    for m in probe.MODELS:
        kinds = [k for mm, k, _ in full if mm == m]
        assert kinds.count("short") == 7 and kinds.count("medium") == 1 and kinds.count("large") == 1
    # With the measured 80-token overhead the on-paper worst case (~20.7K) exceeds the cap; accepted
    # (option a): the runtime guard in test_token_cap_stops_before_overshoot is what prevents overshoot.


def test_call_cap_counts_prior_runs(monkeypatch, log):
    log.write_text("".join(json.dumps({"tokens_charged": 1}) + "\n" for _ in range(49)))
    seen, msgs = [], []
    probe.run("full", make_client(ok_handler(seen)), FAKE_KEY, log, sleep=lambda s: None, out=msgs.append)
    assert len(seen) == 1                                   # 49 prior + 1 = cap of 50
    assert any("call cap" in m for m in msgs)


def test_token_cap_stops_before_overshoot(monkeypatch, log):
    monkeypatch.setattr(probe, "TOKEN_CAP", 560)  # each OK call charges 90; short worst case 393
    seen, msgs = [], []
    probe.run("full", make_client(ok_handler(seen)), FAKE_KEY, log, sleep=lambda s: None, out=msgs.append)
    charged = sum(r["tokens_charged"] for r in rows_of(log))
    assert len(seen) == 2 and charged <= 560                # 3rd call refused: 180 + 393 > 560
    assert any("token cap" in m for m in msgs)


def test_400_marks_model_dead_and_others_continue(log):
    seen = []

    def handler(request):
        model = json.loads(request.content)["model"]
        seen.append(model)
        if model == "qwen/qwen3.8-27b":
            return httpx.Response(400, json={"error": {"message": "reasoning_effort invalid"}})
        return httpx.Response(200, text=OK_STREAM)

    probe.run("full", make_client(handler), FAKE_KEY, log, sleep=lambda s: None, out=lambda m: None)
    assert seen.count("qwen/qwen3.8-27b") == 1
    assert seen.count("openai/gpt-oss-120b") == 9 and seen.count("openai/gpt-oss-20b") == 9
    bad = [r for r in rows_of(log) if r["http_status"] == 400][0]
    assert "reasoning_effort invalid" in bad["error_body"]


@pytest.mark.parametrize("status", [401, 403, 429])
def test_stop_statuses_halt_whole_probe(status, log):
    seen = []

    def handler(request):
        seen.append(1)
        return httpx.Response(status, headers={"retry-after": "7"}, json={"error": {"message": "nope"}})

    probe.run("full", make_client(handler), FAKE_KEY, log, sleep=lambda s: None, out=lambda m: None)
    assert len(seen) == 1
    assert ["retry-after", "7"] in rows_of(log)[0]["response_headers"]


def test_fake_key_never_leaks(log, capsys):
    sent = []

    def handler(request):  # worst case: server echoes the key back in body and headers
        sent.append(request.headers["authorization"])
        hdrs = [("x-echo", FAKE_KEY), ("set-cookie", "__cf_bm=COOKIE_SECRET_VALUE; path=/")]
        if len(sent) == 1:
            return httpx.Response(400, headers=hdrs, text=f"bad key {FAKE_KEY}")
        return httpx.Response(200, headers=hdrs, text=sse(
            {"choices": [{"delta": {"content": f"echo {FAKE_KEY}"}, "finish_reason": "stop"}]}))

    probe.run("full", make_client(handler), FAKE_KEY, log, sleep=lambda s: None)
    text = log.read_text()
    assert sent[0] == f"Bearer {FAKE_KEY}"                  # key really was sent
    assert FAKE_KEY not in text
    assert FAKE_KEY not in capsys.readouterr().out
    assert "***" in text
    assert "COOKIE_SECRET_VALUE" not in text                # cookie value dropped...
    assert ["set-cookie", "<redacted>"] in rows_of(log)[0]["response_headers"]  # ...header name kept


def test_transport_error_logs_type_only_and_charges_worst_case(log):
    def handler(request):
        raise httpx.ReadTimeout(f"timed out with {FAKE_KEY}", request=request)

    probe.run("pilot", make_client(handler), FAKE_KEY, log, sleep=lambda s: None, out=lambda m: None)
    r = rows_of(log)[0]
    assert r["error"] == "ReadTimeout" and FAKE_KEY not in log.read_text()
    assert r["tokens_charged"] == probe.worst_case_tokens(probe.PILOT_Q)


def test_analyze_reports_header_deltas(log):
    rows = [
        {"ts": "2026-11-14T00:00:00+00:00", "call_no": 1, "model": "m", "http_status": 200, "max_tokens": 300,
         "usage": {"total_tokens": 90}, "response_headers": [["x-ratelimit-remaining-tokens", "8000"],
                                                             ["x-ratelimit-remaining-requests", "1000"]]},
        {"ts": "2026-11-14T00:00:24+00:00", "call_no": 4, "model": "m", "http_status": 200, "max_tokens": 300,
         "usage": {"total_tokens": 95}, "response_headers": [["x-ratelimit-remaining-tokens", "7700"],
                                                             ["x-ratelimit-remaining-requests", "999"]]},
    ]
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = []
    probe.analyze(log, out=out.append)
    line = out[1].split()
    assert line[:6] == ["m", "4", "24.0", "95", "300", "1"]


def test_tpm429_stage_sends_8000_and_cap_uses_it(log):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(429, headers={"retry-after": "3"}, json={"error": {"message": "tpm"}})

    # 9,690 already spent (real state) + worst case 8,090 = 17,780 <= 20,000 -> call goes out
    log.write_text(json.dumps({"tokens_charged": 9690}) + "\n")
    probe.run("tpm429", make_client(handler), FAKE_KEY, log, sleep=lambda s: None, out=lambda m: None)
    assert len(bodies) == 1
    assert bodies[0]["model"] == "openai/gpt-oss-20b" and bodies[0]["max_completion_tokens"] == 8000
    assert rows_of(log)[-1]["max_tokens"] == 8000

    # 12,000 spent + 8,090 > 20,000 -> refused before any request
    log.write_text(json.dumps({"tokens_charged": 12000}) + "\n")
    msgs = []
    probe.run("tpm429", make_client(handler), FAKE_KEY, log, sleep=lambda s: None, out=msgs.append)
    assert len(bodies) == 1 and any("token cap" in m for m in msgs)


def test_other_stages_still_use_300(log):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, text=OK_STREAM)

    probe.run("pilot", make_client(handler), FAKE_KEY, log, sleep=lambda s: None, out=lambda m: None)
    assert {b["max_completion_tokens"] for b in bodies} == {300}
