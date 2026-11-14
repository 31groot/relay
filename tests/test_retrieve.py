import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import retrieve  # noqa: E402


def test_tokenize_hyphens_and_decimals():
    toks = retrieve.tokenize("Position-wise FFN reaches 28.4 BLEU; 4.5-5 bytes/chunk.")
    assert {"position", "wise", "positionwise", "ffn", "28.4", "bleu", "4.5", "5"} <= set(toks)
    assert "4.55" not in toks                                   # numeric ranges are not joined
    assert set(retrieve.tokenize("positionwise")) <= set(retrieve.tokenize("position-wise"))


def test_bm25_hand_computed_three_docs():
    # Hand calculation, k1=1.5, b=0.75, query "c". Docs: d0 "a b" (dl 2), d1 "a c c" (dl 3), d2 "b c d e" (dl 4).
    # N=3, df(c)=2 → idf = ln(1 + 1.5/2.5) = ln 1.6 = 0.4700036; avgdl = 3.
    # d1: tf 2 → 2*2.5 / (2 + 1.5*(0.25 + 0.75*3/3)) = 5/3.5   = 1.4285714 → 0.6714337
    # d2: tf 1 → 1*2.5 / (1 + 1.5*(0.25 + 0.75*4/3)) = 2.5/2.875 = 0.8695652 → 0.4086988
    s = retrieve.BM25(["a b", "a c c", "b c d e"]).scores("c")
    assert s[0] == 0
    assert s[1] == pytest.approx(math.log(1.6) * 5 / 3.5) == pytest.approx(0.6714337, abs=1e-6)
    assert s[2] == pytest.approx(math.log(1.6) * 2.5 / 2.875) == pytest.approx(0.4086988, abs=1e-6)


def test_rrf_hand_computed():
    # k=60. item0: 1/61+1/62=0.032522, item2: 1/63+1/61=0.032266, item1: 1/62+1/63=0.032002
    assert list(retrieve.rrf([0, 1, 2], [2, 0, 1])) == [0, 2, 1]


def test_rank_ties_by_index():
    assert list(retrieve.rank([1.0, 2.0, 2.0, 0.5])) == [1, 2, 0, 3]


def test_route_names_only():
    assert retrieve.route("How does Moshi handle overlap?") == {"moshi"}
    assert retrieve.route("What does H-Net's dynamic chunking do?") == {"dynchunk"}
    assert retrieve.route("How many layers does the Transformer encoder have?") is None
