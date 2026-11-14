"""Phase 5 offline tests: auto-grader, corruption functions, runner resume + eval-share guard. No Groq."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "exp"), str(ROOT / "app")]
import corrupt  # noqa: E402
import grade as gr  # noqa: E402
import limiter  # noqa: E402
import pipeline as pl  # noqa: E402
import run_phase5 as rp  # noqa: E402
from test_pipeline import SUPPORTED, FakeRetriever, scripted  # noqa: E402


# ---- grader: number normalisation, NFKC/whitespace, page coverage, refusal outcomes ----

def test_number_normalisation():
    assert gr.has_term("a context length of 4,096 tokens", "4096")
    assert gr.has_term("2,000 hours of phone conversations", "2000")
    assert gr.has_term("4 096 tokens", "4096")
    assert gr.has_term("uses 4000 warmup steps", "4,000")
    assert not gr.has_term("40,96", "4096")                       # not a thousands separator


def test_nfkc_whitespace_case_and_dashes():
    assert gr.has_term("predicting time‑aligned  text\ntokens", "time-aligned text tokens")  # U+2011 hyphen, spaces
    assert gr.has_term("\U0001D45D\U0001D461 ≥ 0.5", "pt≥0.5")                           # math italic → NFKC
    assert gr.has_term("Overtakes the PERPLEXITY", "perplexity")


def chunk(cid, paper, ps, pe, text="x"):
    return {"chunk_id": cid, "paper": paper, "page_start": ps, "page_end": pe, "text": text}


CHUNKS = {"a1": chunk("a1", "attention", 7, 8, "with warmup_steps = 4000 and Adam"), "a2": chunk("a2", "attention", 9, 9),
          "m1": chunk("m1", "moshi", 7, 7)}
Q_ANS = {"id": "A02", "expected": "answer", "subtype": "answerable", "paper": "attention", "must_include": ["4000"],
         "evidence_page": 7, "evidence_phrase": "warmup_steps = 4000"}
Q_REF = {"id": "A08", "expected": "refuse", "subtype": "off-topic", "paper": "all"}


def res(outcome, text="", labels=(), retrieved=("a1", "a2", "m1")):
    attempts = [{"normalized_output": text, "cited_labels": list(labels)}] if text else []
    return {"outcome": outcome, "attempts": attempts, "retrieved": [{"chunk_id": c} for c in retrieved]}


def test_page_range_coverage():
    assert gr.covers(CHUNKS["a1"], "attention", 7) and gr.covers(CHUNKS["a1"], "attention", 8)
    assert not gr.covers(CHUNKS["a1"], "attention", 9) and not gr.covers(CHUNKS["m1"], "attention", 7)


def test_grades_answerable():
    assert gr.grade(Q_ANS, res("answered", "It used 4,000 warmup steps [S1].", ["S1"]), CHUNKS)["auto_grade"] == "correct"
    g = gr.grade(Q_ANS, res("answered", "It used 4,000 warmup steps [S2].", ["S2"]), CHUNKS)   # cites p.9, not p.7
    assert g["auto_grade"] == "wrong" and g["cites_evidence_page"] is False
    g = gr.grade(Q_ANS, res("answered", "It used many warmup steps [S1].", ["S1"]), CHUNKS)
    assert g["auto_grade"] == "wrong" and g["missing_terms"] == ["4000"]
    assert gr.grade(Q_ANS, res("refused", "NOT_IN_PAPERS"), CHUNKS)["auto_grade"] == "refused_wrongly"
    assert gr.grade(Q_ANS, res("refused"), CHUNKS)["evidence_in_context"] is True       # a1 holds the evidence phrase
    assert gr.grade(Q_ANS, res("refused", retrieved=("a2", "m1")), CHUNKS)["evidence_in_context"] is False


def test_label_digits_never_satisfy_a_term():
    q = dict(Q_ANS, must_include=["1"])
    assert gr.grade(q, res("answered", "Warmup is described [S1].", ["S1"]), CHUNKS)["auto_grade"] == "wrong"


def test_grades_refusal_outcomes():
    assert gr.grade(Q_REF, res("refused"), CHUNKS)["auto_grade"] == "refused_correctly"          # gate / NOT_IN_PAPERS
    assert gr.grade(Q_REF, res("answered", "Paris [S1].", ["S1"]), CHUNKS)["auto_grade"] == "wrong"
    assert gr.grade(Q_REF, res("error"), CHUNKS)["auto_grade"] == "error"                       # not a refusal
    assert gr.grade(Q_REF, res("refused"), CHUNKS)["evidence_in_context"] is None


# ---- corruption functions ----

def test_number_swap():
    assert corrupt.number_swap("Adam [S1] and 4000 warmup steps [S2].") == "Adam [S1] and 5000 warmup steps [S2]."
    assert corrupt.number_swap("Pdrop = 0.1 [S2]") == "Pdrop = 1.1 [S2]"
    assert corrupt.number_swap("It has 9 layers [S3].") == "It has 1 layers [S3]."
    assert corrupt.number_swap("No numbers here [S4].") is None                # label digits are not touched


def test_citation_swap_points_away_from_evidence():
    texts = ["warmup_steps = 4000 here", "also warmup_steps = 4000", "unrelated text", "more text"]
    out = corrupt.citation_swap("Warmup is 4000 [S1][S2].", texts, "warmup_steps = 4000")
    assert out == "Warmup is 4000 [S3][S3]."
    assert corrupt.citation_swap("x [S1].", ["warmup_steps = 4000"], "warmup_steps = 4000") is None


def test_unsupported_addition_passes_deterministic_check():
    out = corrupt.unsupported_addition("Warmup is 4000 [S2].")
    assert out == f"Warmup is 4000 [S2]. {corrupt.FABRICATED} [S2]."
    assert pl.deterministic_check(out, 8) == (True, "ok")
    assert corrupt.unsupported_addition("no labels") is None


@pytest.mark.skipif(not (ROOT / "data" / "index" / "chunks.jsonl").exists(), reason="index missing")
def test_fabricated_claim_absent_from_library():
    texts = [json.loads(line)["text"] for line in (ROOT / "data" / "index" / "chunks.jsonl").read_text().splitlines()]
    assert not any(gr.has_term(t, "TPU v4") or gr.has_term(t, "512 TPU") for t in texts)


# ---- runner: resume + eval-share guard counted from the log ----

QS = [{"id": f"A0{i}", "expected": "answer", "subtype": "answerable", "paper": "moshi", "question": f"question {i}",
       "must_include": ["x"], "evidence_page": 11, "evidence_phrase": "Passage 1"} for i in (1, 2, 3)]


def chunks_by_id():
    from test_pipeline import ch
    return {ch(i)["chunk_id"]: ch(i) for i in range(1, 9)}


def make_pipeline(log, gens, judges, budget, reqs):
    return pl.Pipeline(FakeRetriever({None: 0.8}), scripted(gens, judges, reqs), "gsk_FAKE_p5_key", budget=budget,
                       log_path=log, pacer=limiter.Pacer(sleep=lambda s: None))


def test_resume_skips_finished_and_guard_counts_log(tmp_path):
    log, reqs = tmp_path / "phase5_log.jsonl", []
    day = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date().isoformat()
    p = make_pipeline(log, ["Fact x [S1]."] * 2, [SUPPORTED] * 2, rp.make_budget(log, day), reqs)
    rp.run("default", QS[:2], p, log, chunks_by_id(), out=lambda m: None)
    assert len(reqs) == 4 and [q["id"] for q in rp.pending(QS, "default", log)] == ["A03"]
    assert rp.pending(QS, "baseline", log) == QS                          # resume is per setting
    budget2 = rp.make_budget(log, day)                                    # a "new run" reads usage back from the log
    assert budget2.used == {pl.GEN_MODEL: 1000, pl.JUDGE_MODEL: 1000}     # 2 × 500 per model (mock usage)


def test_budget_refusal_is_not_finished_and_stops(tmp_path):
    log, reqs = tmp_path / "phase5_log.jsonl", []
    tiny = pl.Budget(token_cap=10, call_cap=80)                           # nothing fits
    p = make_pipeline(log, [], [], tiny, reqs)
    msgs = []
    out = rp.run("default", QS, p, log, chunks_by_id(), out=msgs.append)
    assert out == [] and reqs == [] and any("STOP" in m for m in msgs)    # stopped after the first refusal
    assert [q["id"] for q in rp.pending(QS, "default", log)] == ["A01", "A02", "A03"]  # will run again on resume


def test_cli_requires_go_real_and_setting():
    with pytest.raises(SystemExit, match="usage"):
        rp.main(["--setting", "default"])


# ---- judge limit 400 + truncation flag + re-judge of truncated items ----

def judge_client(judge_replies, reqs, gen_text="Fact x [S1]."):
    """Generator always answers gen_text; judge replies are (text, finish_reason) in order."""
    import httpx
    replies = list(judge_replies)

    def handler(request):
        body = json.loads(request.content)
        reqs.append(body)
        if body["model"] == pl.GEN_MODEL:
            text, finish = gen_text, "stop"
        else:
            text, finish = replies.pop(0)
        sse = (f"data: {json.dumps({'choices': [{'delta': {'content': text}, 'finish_reason': finish}]})}\n\n"
               f"data: {json.dumps({'choices': [], 'usage': {'total_tokens': 300}})}\n\ndata: [DONE]\n\n")
        return httpx.Response(200, text=sse)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_truncated_judge_is_flagged_not_unsupported(tmp_path):
    reqs = []
    p = pl.Pipeline(FakeRetriever({None: 0.8}), judge_client([("", "length")], reqs), "gsk_FAKE_x",
                    log_path=tmp_path / "log.jsonl", pacer=limiter.Pacer(sleep=lambda s: None))
    res = p.answer("q", "default:A01")
    assert [b["model"] for b in reqs] == [pl.GEN_MODEL, pl.JUDGE_MODEL]          # no correction call
    assert reqs[1]["max_completion_tokens"] == 400
    assert res["attempts"][0]["judge"]["verdict"] == "judge_truncated"
    assert res["outcome"] == "refused" and res["outcome_reason"].startswith("judge_truncated")
    assert pl.judge_verdict({"http_status": 200, "finish_reason": "stop", "text": "not json"})["verdict"] == "unsupported"


def test_rejudge_reuses_logged_generation(tmp_path):
    log, reqs = tmp_path / "phase5_log.jsonl", []
    p = pl.Pipeline(FakeRetriever({None: 0.8}), judge_client([("", "length"), (SUPPORTED, "stop")], reqs), "gsk_FAKE_x",
                    log_path=log, pacer=limiter.Pacer(sleep=lambda s: None))
    p.answer("q", "default:A01")
    assert rp.truncated_judges(log, "default") == ["default:A01"]
    old = rp.latest_requests(log)["default:A01"]
    new = rp.rejudge(p, old, chunks_by_id())
    assert [b["model"] for b in reqs] == [pl.GEN_MODEL, pl.JUDGE_MODEL, pl.JUDGE_MODEL]   # NO new generation
    assert reqs[2]["messages"] == reqs[1]["messages"] and reqs[2]["max_completion_tokens"] == 400
    assert new["outcome"] == "answered" and new["rejudged"] and new["attempts"][0]["judge_old"]["verdict"] == "judge_truncated"
    assert rp.latest_requests(log)["default:A01"]["rejudged"] and rp.truncated_judges(log, "default") == []
    assert "default:A01" in rp.finished(log)


# ---- corruption-test runner ----

import run_corruption as rc  # noqa: E402


def test_build_items_from_default_answers(tmp_path):
    log, reqs = tmp_path / "phase5_log.jsonl", []
    p = pl.Pipeline(FakeRetriever({None: 0.8}), judge_client([(SUPPORTED, "stop")], reqs, gen_text="It has 4000 steps [S1]."),
                    "gsk_FAKE_x", log_path=log, pacer=limiter.Pacer(sleep=lambda s: None))
    p.answer("q", "default:A01")
    qs = {"A01": {"evidence_phrase": "Passage 1"}}
    items = rc.build_items(log, qs, chunks_by_id())
    kinds = {i["kind"]: i for i in items}
    assert [i["kind"] for i in items][:4] == ["control", "number_swap", "citation_swap", "unsupported_addition"]
    assert kinds["number_swap"]["text"] == "It has 5000 steps [S1]." and kinds["control"]["expected"] == "supported"
    assert kinds["citation_swap"]["text"] == "It has 4000 steps [S2]."              # S2 lacks "Passage 1"
    assert kinds["lead_default_A01_released"]["expected"] == "contested"


def test_summarize_excludes_truncated():
    rows = [{"kind": "number_swap", "verdict": v} for v in ("unsupported", "supported", "judge_truncated", "unsupported")] \
         + [{"kind": "control", "verdict": "supported"}, {"kind": "control", "verdict": "unsupported"}]
    s = rc.summarize(rows)
    assert s["number_swap"] == (2, 3, 1) and s["control"] == (1, 2, 0)            # caught 2 of 3 scored, 1 truncated
