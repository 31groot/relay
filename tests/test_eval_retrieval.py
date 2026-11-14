import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "exp"))
import eval_retrieval as ev  # noqa: E402

Q = {"id": "R01", "paper": "attention", "page": 3, "key_phrase": "N = 6 identical layers", "alt_phrases": ["dmodel = 512"]}


def chunk(paper="attention", ps=2, pe=3, text="The encoder is composed of a stack of N = 6\nidentical layers."):
    return {"paper": paper, "page_start": ps, "page_end": pe, "text": text}


def test_hit_rule_all_three_conditions():
    assert ev.is_hit(chunk(), Q)
    assert not ev.is_hit(chunk(paper="moshi"), Q)                     # wrong paper
    assert not ev.is_hit(chunk(ps=4, pe=5), Q)                        # stated page outside span
    assert not ev.is_hit(chunk(text="six identical layers"), Q)       # phrase absent
    assert ev.is_hit(chunk(text="outputs of dimension dmodel  =  512"), Q)  # alt phrase, whitespace-insensitive


@pytest.mark.skipif(not (ROOT / "data" / "clean").exists(), reason="run app/ingest.py first")
def test_attribution_partitions_real_multipage_chunks():
    att = ev.Attribution()
    rows = [json.loads(line) for line in (ROOT / "data" / "index" / "chunks.jsonl").read_text().splitlines()]
    multi = [c for c in rows if c["page_end"] > c["page_start"]][:25]
    assert multi
    for c in multi:
        parts = [att.page_text(c, p) for p in range(c["page_start"], c["page_end"] + 1)]
        assert " ".join(p for p in parts if p).split() == c["text"].split()   # every word attributed to exactly one page
        assert parts[0] and parts[-1]                                          # both end pages contribute


def test_first_rank():
    assert ev.first_rank([5, 3, 9], {9, 3}) == 2 and ev.first_rank([1, 2], {7}) is None
