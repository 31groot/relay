import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "exp"))
import validate_questions as v  # noqa: E402


def test_whitespace_insensitive_match_after_nfkc():
    page = "vectors where bt = 1 are retained in the compressed\nsequence; {pt ≥0.5} and dmodel /h = 64."
    assert v.contains(page, "vectors where 𝑏𝑡= 1 are retained")   # math-italic + different spacing
    assert v.contains(page, "𝑝𝑡≥0.5")
    assert v.contains(page, "dmodel/h = 64")
    assert v.contains(page, "compressed sequence")                  # across a line break
    assert not v.contains(page, "bt = 2")


@pytest.fixture
def fake_library(tmp_path, monkeypatch):
    for kind, text in (("clean", {1: "alpha beta", 2: "", 3: "gamma"}), ("raw", {1: "alpha beta", 2: "delta", 3: "gamma"})):
        d = tmp_path / "data" / kind / v.PAPER_IDS["attention"]
        d.mkdir(parents=True)
        for n, t in text.items():
            (d / f"p{n:03d}.txt").write_text(t)
        for other in ("moshi", "dynchunk"):
            (tmp_path / "data" / kind / v.PAPER_IDS[other]).mkdir(parents=True)
    monkeypatch.setattr(v, "ROOT", tmp_path)


def rq(qid, page, phrase):
    return {"id": qid, "paper": "attention", "type": "fact", "question": "?", "page": page, "key_phrase": phrase}


def test_statuses_and_near_miss_empty_page(fake_library):
    qs = {"retrieval_questions": [rq("R1", 1, "alpha  beta"), rq("R2", 1, "gamma"), rq("R3", 1, "delta"),
                                  rq("R4", 1, "omega"), rq("R5", 99, "alpha")],
          "answer_questions": [{"id": "A1", "expected": "refuse", "subtype": "near-miss", "paper": "attention",
                                "question": "?", "why_unanswerable": "x", "near_miss_page": 2}]}
    errors, rows = v.validate(qs)
    status = {r[0]: r[3] for r in rows}
    assert status == {"R1": "ok", "R2": "elsewhere", "R3": "raw-only", "R4": "missing"}
    assert any("R5" in e and "outside" in e for e in errors)
    assert any("A1" in e and "no clean text" in e for e in errors)
