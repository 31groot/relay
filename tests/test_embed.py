import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import embed  # noqa: E402

pytestmark = pytest.mark.skipif(not (embed.MODEL_DIR / "model.onnx").exists(), reason="model not downloaded")


@pytest.fixture(scope="module")
def e():
    return embed.Embedder()


def test_similar_pairs_score_clearly_higher(e):
    pairs = [
        (True, "The encoder is a stack of six identical layers.", "Six identical layers are stacked to form the encoder."),
        (True, "The model was trained on eight GPUs for twelve hours.", "Training took twelve hours using 8 graphics processors."),
        (False, "The encoder is a stack of six identical layers.", "How do I make a sourdough starter from scratch?"),
        (False, "The model was trained on eight GPUs for twelve hours.", "The capital of France is Paris."),
    ]
    v, _ = e.embed_passages([s for _, a, b in pairs for s in (a, b)])
    cos = [float(v[2 * i] @ v[2 * i + 1]) for i in range(len(pairs))]
    similar = [c for (s, *_), c in zip(pairs, cos) if s]
    unrelated = [c for (s, *_), c in zip(pairs, cos) if not s]
    assert min(similar) > max(unrelated) + 0.3, cos     # measured 2026-11-14: 0.976, 0.883 vs 0.384, 0.397


def test_long_text_split_into_equal_pieces_within_window(e):
    text = " ".join(["attention"] * 700)                  # ~700 content tokens → 2 pieces of ~350, not 510 + 190
    ps = e.pieces(text)
    assert len(ps) == 2 and all(len(p) <= embed.WINDOW for p in ps)
    assert abs(len(ps[0]) - len(ps[1])) <= 1
    assert all(p[0] == e.cls and p[-1] == e.sep for p in ps)
    v, n = e.embed_passages([text, "short"])
    assert n == [2, 1] and np.allclose(np.linalg.norm(v, axis=1), 1, atol=1e-5)


def test_query_instruction_on_queries_only(e):
    q = e.embed_queries(["How many encoder layers?"])
    p, _ = e.embed_passages(["How many encoder layers?"])
    assert not np.allclose(q, p, atol=1e-3)               # instruction changes the query vector
    with_instr, _ = e.embed_passages([embed.QUERY_INSTRUCTION + "How many encoder layers?"])
    assert np.allclose(q, with_instr, atol=1e-5)          # ...and is exactly the prefix, nothing else
