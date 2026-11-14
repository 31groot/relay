"""Phase 4 pipeline, fully offline: httpx.MockTransport stands in for Groq, a fake retriever for the index."""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
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
                    budget=budget, log_path=tmp_path / "log.jsonl")
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
                    log_path=tmp_path / "log.jsonl")
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
