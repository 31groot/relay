"""Phase 4 pipeline, fully offline: httpx.MockTransport stands in for Groq, a fake retriever for the index."""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import limiter  # noqa: E402
import llm  # noqa: E402
import pipeline as pl  # noqa: E402
import prompts  # noqa: E402

FAKE_KEY = "gsk_FAKE_SECRET_pipeline_456"


def ch(i, paper="moshi", text=None):
    return {"chunk_id": f"{paper}-200-{i:04d}", "paper": paper, "page_start": 10 + i, "page_end": 10 + i,
            "section_heading": f"3.{i} Section", "text": text or f"Passage {i} text about topic {i}."}


class FakeRetriever:
    def __init__(self, scores):           # scores: {papers_key: gate score}; papers_key None = all papers
        self.scores, self.searches = scores, []

    def search(self, question, papers=None):
        key = None if papers is None else tuple(sorted(papers))
        self.searches.append(key)
        return [(ch(i), i) for i in range(1, 9)], self.scores[key]


def sse(text):
    return (f"data: {json.dumps({'choices': [{'delta': {'content': text}, 'finish_reason': 'stop'}]})}\n\n"
            f"data: {json.dumps({'choices': [], 'usage': {'total_tokens': 500, 'prompt_tokens': 400, 'completion_tokens': 100}})}\n\n"
            "data: [DONE]\n\n")


def scripted(gen_outputs, judge_outputs, requests):
    gens, judges = list(gen_outputs), list(judge_outputs)

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        out = gens.pop(0) if body["model"] == pl.GEN_MODEL else judges.pop(0)
        return httpx.Response(200, text=sse(out), headers=[("set-cookie", "__cf_bm=COOKIE_VAL"), ("x-echo", FAKE_KEY)])
    return httpx.Client(transport=httpx.MockTransport(handler))


def run(question, gens, judges, scores=None, budget=None, tmp_path=None):
    reqs = []
    p = pl.Pipeline(FakeRetriever(scores or {None: 0.8}), scripted(gens, judges, reqs), FAKE_KEY,
                    budget=budget, log_path=tmp_path / "log.jsonl", pacer=limiter.Pacer(sleep=lambda s: None))
    return p.answer(question, "T1"), reqs, p


SUPPORTED = '{"verdict": "supported", "claim": "", "reason": "all claims stated"}'
UNSUPPORTED = '{"verdict": "unsupported", "claim": "It runs at 13 Hz.", "reason": "S2 says 12.5 Hz"}'


# ---- deterministic check ----

@pytest.mark.parametrize("text,ok", [
    ("Mimi runs at 12.5 Hz [S2].", True),
    ("First fact [S1][S4].\n\nSecond fact [S1, S3].", True),
    ("NOT_IN_PAPERS", True),
    ("Mimi runs at 12.5 Hz [S9].", False),                         # out-of-range label (8 sources)
    ("Mimi runs at 12.5 Hz [S0].", False),
    ("Cited fact [S1].\n\nUncited second paragraph.", False),      # paragraph without citation
    ("NOT_IN_PAPERS, but maybe 12 Hz [S1].", False),               # refusal mixed with text
    ("", False),
])
def test_deterministic_check(text, ok):
    assert pl.deterministic_check(text, 8)[0] is ok


def test_render_labels_to_citations():
    chunks = [ch(1, "attention"), ch(2)]
    assert pl.render("Fact [S2]. Both [S1][S2].", chunks) == (
        "Fact [Moshi, §3.2 Section, p.12]. Both [Attention Is All You Need, §3.1 Section, p.11][Moshi, §3.2 Section, p.12].")


def test_parse_verdict_unparseable_is_unsupported():
    assert pl.parse_verdict("looks fine to me")["verdict"] == "unsupported"
    assert pl.parse_verdict('{"verdict": "maybe"}')["parsed"] is False
    assert pl.parse_verdict('ok: {"verdict": "supported", "claim": "", "reason": "x"}')["verdict"] == "supported"


# ---- pipeline paths ----

def test_gate_refusal_makes_zero_calls(tmp_path):
    res, reqs, _ = run("What is the capital of France?", [], [], scores={None: 0.45}, tmp_path=tmp_path)
    assert res["outcome"] == "refused" and res["outcome_reason"] == "gate" and reqs == []


def test_routed_below_gate_is_broadened_before_refusing(tmp_path):
    res, reqs, p = run("How does Moshi compare?", ["Fact [S1]."], [SUPPORTED],
                       scores={("moshi",): 0.55, None: 0.72}, tmp_path=tmp_path)
    assert p.retriever.searches == [("moshi",), None]                  # routed search, then all papers
    assert res["broadened"] and res["outcome"] == "answered"
    res2, reqs2, p2 = run("How does Moshi compare?", [], [], scores={("moshi",): 0.55, None: 0.50}, tmp_path=tmp_path)
    assert p2.retriever.searches == [("moshi",), None] and res2["outcome_reason"] == "gate" and reqs2 == []


def test_not_in_papers_skips_judge(tmp_path):
    res, reqs, _ = run("q", ["NOT_IN_PAPERS"], [], tmp_path=tmp_path)
    assert len(reqs) == 1 and res["outcome"] == "refused" and "judge skipped" in res["outcome_reason"]


def test_happy_path_judge_sees_only_cited_chunks_and_labels_logged(tmp_path):
    res, reqs, _ = run("q", ["Fact A [S2]. Fact B [S5]."], [SUPPORTED], tmp_path=tmp_path)
    judge_body = reqs[1]["messages"][1]["content"]
    assert "Passage 2 text" in judge_body and "Passage 5 text" in judge_body
    assert all(f"Passage {i} text" not in judge_body for i in (1, 3, 4, 6, 7, 8))
    assert res["attempts"][0]["cited_labels"] == ["S2", "S5"]
    logged = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert logged[-1]["attempts"][0]["cited_labels"] == ["S2", "S5"]
    assert res["final_answer"] == "Fact A [Moshi, §3.2 Section, p.12]. Fact B [Moshi, §3.5 Section, p.15]."


def test_correction_path_then_supported(tmp_path):
    res, reqs, _ = run("q", ["It runs at 13 Hz [S2].", "It runs at 12.5 Hz [S2]."], [UNSUPPORTED, SUPPORTED], tmp_path=tmp_path)
    assert [b["model"] for b in reqs] == [pl.GEN_MODEL, pl.JUDGE_MODEL, pl.GEN_MODEL, pl.JUDGE_MODEL]
    corr = reqs[2]["messages"]
    assert corr[:2] == reqs[0]["messages"]                                 # SAME context
    assert corr[2] == {"role": "assistant", "content": "It runs at 13 Hz [S2]."} and "It runs at 13 Hz." in corr[3]["content"]
    assert res["outcome"] == "answered" and len(res["attempts"]) == 2


def test_correction_fails_again_refuses(tmp_path):
    res, reqs, _ = run("q", ["A [S2].", "B [S2]."], [UNSUPPORTED, UNSUPPORTED], tmp_path=tmp_path)
    assert len(reqs) == 4 and res["outcome"] == "refused" and res["outcome_reason"] == "failed after one correction"


def test_deterministic_failure_skips_judge_then_corrects(tmp_path):
    res, reqs, _ = run("q", ["Fact [S9].", "Fact [S1]."], [SUPPORTED], tmp_path=tmp_path)
    assert [b["model"] for b in reqs] == [pl.GEN_MODEL, pl.GEN_MODEL, pl.JUDGE_MODEL]
    assert "out of range" in res["attempts"][0]["check"] and res["outcome"] == "answered"


def test_budget_guard_refuses_instead_of_exceeding(tmp_path):
    # cap = exactly the first generate's worst case: it fits; after 500 charged, the (larger) correction cannot.
    cap = pl.worst_case(prompts.generator_messages("q", [ch(i) for i in range(1, 9)]), pl.GEN_MAX)
    budget = pl.Budget(token_cap=cap)
    res, reqs, _ = run("q", ["It runs at 13 Hz [S2].", "x"], [UNSUPPORTED], budget=budget, tmp_path=tmp_path)
    assert len(reqs) == 2 and res["outcome_reason"] == "budget"            # correction refused, not sent
    assert all(v <= cap for v in budget.used.values())
    budget0 = pl.Budget(token_cap=cap - 1)                                  # first call itself doesn't fit → no call at all
    res0, reqs0, _ = run("q", ["x"], [], budget=budget0, tmp_path=tmp_path)
    assert reqs0 == [] and res0["outcome_reason"] == "budget"


def test_call_cap(tmp_path):
    budget = pl.Budget(call_cap=1)
    res, reqs, _ = run("q", ["Fact [S1]."], [SUPPORTED], budget=budget, tmp_path=tmp_path)
    assert len(reqs) == 1 and res["outcome_reason"] == "budget"


def test_429_stops_run(tmp_path):
    def handler(request):
        return httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "rate limit"}})
    p = pl.Pipeline(FakeRetriever({None: 0.8}), httpx.Client(transport=httpx.MockTransport(handler)), FAKE_KEY,
                    log_path=tmp_path / "log.jsonl", pacer=limiter.Pacer(sleep=lambda s: None))
    with pytest.raises(llm.Stopped):
        p.answer("q", "T1")


def test_key_and_cookie_never_logged(tmp_path, capsys):
    res, reqs, _ = run("q", ["Fact [S1]."], [SUPPORTED], tmp_path=tmp_path)
    pl.show(res)
    log = (tmp_path / "log.jsonl").read_text()
    assert FAKE_KEY not in log and FAKE_KEY not in capsys.readouterr().out and "COOKIE_VAL" not in log
    assert "***" in log                                                     # echoed key was redacted


def test_cli_refuses_without_go_real():
    with pytest.raises(SystemExit, match="go-real"):
        pl.main(["What is Moshi?"])


# ---- Phase 4 run-2 changes: citation normalization, pacing, org redaction, prior usage ----

# Exact raw generator outputs logged in results/phase4_log.jsonl (run 1, 2026-11-14).
RAW_P4_1 = "The Transformer authors trained their models with the Adam optimizer\u202f[\u200bS1] and used 4000 warmup steps during learning\u2011rate scheduling\u202f[\u200bS2]."
RAW_P4_1B = "The Transformer authors trained their models with the Adam optimizer\u202f[\u200bS1]. They used 4000 warmup steps for the learning\u2011rate schedule\u202f[\u200bS2]."
RAW_P4_2 = "The base Transformer model used a dropout rate of\u202fPdrop\u202f=\u202f0.1\u3010S2\u3011"


@pytest.mark.parametrize("raw", [RAW_P4_1, RAW_P4_1B, RAW_P4_2])
def test_logged_raw_outputs_fail_raw_pass_normalized(raw):
    assert pl.deterministic_check(raw, 8)[0] is False                    # what run 1 saw
    norm = pl.normalize_output(raw)
    assert pl.deterministic_check(norm, 8) == (True, "ok")
    assert "\u200b" not in norm and "\u202f" not in norm and "\u3010" not in norm


def test_normalize_exact_mappings():
    assert pl.normalize_output("a\u202fb\u00a0c[\u200bS1]\u3010S2\u3011\ufeff") == "a b c[S1][S2]"
    assert pl.normalize_output("learning\u2011rate") == "learning\u2011rate"   # non-breaking hyphen is Pd, kept


def test_render_uses_normalized_text(tmp_path):
    res, reqs, _ = run("q", [RAW_P4_2], [SUPPORTED], tmp_path=tmp_path)
    assert res["final_answer"] == "The base Transformer model used a dropout rate of Pdrop = 0.1[Moshi, \u00a73.2 Section, p.12]"


def test_format_only_failure_costs_no_correction(tmp_path):
    res, reqs, _ = run("q", [RAW_P4_1], [SUPPORTED], tmp_path=tmp_path)
    assert [b["model"] for b in reqs] == [pl.GEN_MODEL, pl.JUDGE_MODEL]   # straight to the judge
    assert res["attempts"][0]["format_only_fix"] is True and res["outcome"] == "answered"
    assert "\u200b" not in reqs[1]["messages"][1]["content"]               # judge sees the normalized answer


def test_real_failure_after_normalization_still_corrects(tmp_path):
    res, reqs, _ = run("q", ["Fact\u202f[\u200bS9].", "Fact [S1]."], [SUPPORTED], tmp_path=tmp_path)
    assert [b["model"] for b in reqs] == [pl.GEN_MODEL, pl.GEN_MODEL, pl.JUDGE_MODEL]
    assert res["attempts"][0]["format_only_fix"] is False and "out of range" in res["attempts"][0]["check"]


def test_pacer_fake_clock():
    t = [100.0]
    slept = []
    p = limiter.Pacer(clock=lambda: t[0], sleep=lambda s: (slept.append(s), t.__setitem__(0, t[0] + s)))
    assert p.wait("m", 2600) == pytest.approx(2600 * 60 / 8000, abs=1e-3)       # unknown = empty bucket at start: 19.5 s
    p.update("m", [["x-ratelimit-remaining-tokens", "797"], ["x-ratelimit-limit-tokens", "8000"]])  # the real 429 headers
    assert p.wait("m", 2600) == pytest.approx((2600 - 797) * 60 / 8000, abs=1e-3)  # 13.5 s, close to Groq's retry-after 13
    assert p.wait("m", 1000) == pytest.approx(1000 * 60 / 8000, abs=1e-3)         # previous reservation used everything
    t[0] += 600                                                          # long idle: bucket caps at the limit
    assert p.available("m") == 8000 and p.wait("m", 9000) == 0           # larger than bucket: needs only a full one


def test_org_id_redacted_in_log(tmp_path):
    def handler(request):
        return httpx.Response(429, json={"error": {"message": "Rate limit reached for model in organization `org_01ABCdef234` on TPM"}})
    p = pl.Pipeline(FakeRetriever({None: 0.8}), httpx.Client(transport=httpx.MockTransport(handler)), FAKE_KEY,
                    log_path=tmp_path / "log.jsonl", pacer=limiter.Pacer(sleep=lambda s: None))
    with pytest.raises(llm.Stopped):
        p.answer("q", "T1")
    log = (tmp_path / "log.jsonl").read_text()
    assert "org_01ABCdef234" not in log and "org_<redacted>" in log


def test_prior_usage_from_log(tmp_path):
    rows = [{"type": "call", "ts": "2026-11-14T13:00:00", "model": "g", "http_status": 200, "usage": {"total_tokens": 2400}, "worst_case": 2483},
            {"type": "call", "ts": "2026-11-14T13:01:00", "model": "g", "http_status": 429, "usage": None, "worst_case": 2489},
            {"type": "call", "ts": "2026-11-14T13:02:00", "model": "j", "http_status": None, "usage": None, "worst_case": 1300},
            {"type": "call", "ts": "2026-11-13T13:00:00", "model": "g", "http_status": 200, "usage": {"total_tokens": 999}, "worst_case": 1},
            {"type": "request"}]
    (tmp_path / "log.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert pl.prior_usage(tmp_path / "log.jsonl", "2026-11-14") == {"g": 2400, "j": 1300}   # 429 = 0, other day ignored


def test_budget_caps_with_prior_usage_and_borrow():
    b = pl.Budget(used={"g": 7482}, caps={"g": 18000})
    assert b.allows("g", 10518) and not b.allows("g", 10519)
    assert b.allows("j", 10000) and not b.allows("j", 10001)              # no borrow → smoke 10K


# ---- Phase 4 fixes: stray brackets, recalibrated estimator ----

def test_stray_bracket_logged_not_failed(tmp_path):
    norm = pl.normalize_output(RAW_P4_2_RUN2)
    assert pl.stray_brackets(norm) == ["Pdrop = 0.1"] and pl.deterministic_check(norm, 8) == (True, "ok")
    assert pl.stray_brackets("A [S1]. B [S2, S3].") == []                  # citations are not stray
    res, reqs, _ = run("q", [RAW_P4_2_RUN2], [SUPPORTED], tmp_path=tmp_path)
    assert res["outcome"] == "answered" and res["attempts"][0]["stray_brackets"] == ["Pdrop = 0.1"]
    logged = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert logged[-1]["attempts"][0]["stray_brackets"] == ["Pdrop = 0.1"]


# Exact P4-2 output from the paced run (results/phase4_log.jsonl, run 2).
RAW_P4_2_RUN2 = "The base Transformer model used a dropout rate of\u202f0.1\u202f[Pdrop\u202f=\u202f0.1]\u3010S2\u3011"

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not (ROOT / "results" / "phase4_log.jsonl").exists(), reason="Phase 4 log not present")
def test_estimate_covers_actual_prompt_tokens_for_every_logged_call():
    """Rebuild every logged call's exact messages and check chars/CHARS_PER_TOKEN_EST + overhead >= actual prompt tokens."""
    chunks = {c["chunk_id"]: c for c in map(json.loads, (ROOT / "data" / "index" / "chunks.jsonl").read_text().splitlines())}
    rows = [json.loads(line) for line in (ROOT / "results" / "phase4_log.jsonl").read_text().splitlines()]
    checked = 0
    for i, r in enumerate(rows):
        if r["type"] != "call" or not r.get("usage"):
            continue
        req = next(q for q in rows[i:] if q["type"] == "request" and q["request_id"] == r["request_id"])
        cs = [chunks[x["chunk_id"]] for x in req["retrieved"]]
        kind, n = r["step"].split("#")
        gen = prompts.generator_messages(req["question"], cs)
        if kind == "generate":
            first = req["attempts"][0]
            msgs = gen if n == "1" else prompts.correction_messages(gen, first["raw_output"], "", f"deterministic check failed: {first['check']}")
        else:
            a = req["attempts"][int(n) - 1]
            msgs = prompts.judge_messages(req["question"], a.get("normalized_output", a["raw_output"]),
                                          [(int(lab[1:]), cs[int(lab[1:]) - 1]) for lab in a["cited_labels"]])
        chars = sum(len(m["content"]) for m in msgs)
        assert int(chars / pl.CHARS_PER_TOKEN_EST) + pl.TEMPLATE_OVERHEAD >= r["usage"]["prompt_tokens"], (r["request_id"], r["step"])
        assert pl.worst_case(msgs, r["max_tokens"]) >= r["usage"]["total_tokens"]
        checked += 1
    assert checked == 8
