"""bge-small-en-v1.5 via onnxruntime, following the model's own config: CLS pooling, then L2-normalize.

  python app/embed.py   →  data/index/embeddings.npy (row i = line i of data/index/chunks.jsonl)
                           data/index/embeddings_ids.json (chunk_id per row, checked on load)
"""
import json
import math
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "data" / "models" / "bge-small-en-v1.5"
INDEX = ROOT / "data" / "index"
WINDOW = 512                      # max_position_embeddings / max_seq_length from the model config
QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "  # queries only, never chunks


class Embedder:
    def __init__(self, model_dir=MODEL_DIR):
        self.tok = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.sess = ort.InferenceSession(str(model_dir / "model.onnx"), providers=["CPUExecutionProvider"])
        self.cls, self.sep = self.tok.token_to_id("[CLS]"), self.tok.token_to_id("[SEP]")

    def pieces(self, text):
        """Token-id sequences ≤ WINDOW each. Long texts are split into EQUAL pieces (no tiny tail piece)."""
        ids = self.tok.encode(text, add_special_tokens=False).ids
        n = max(1, math.ceil(len(ids) / (WINDOW - 2)))
        size = max(1, math.ceil(len(ids) / n))
        return [[self.cls] + ids[i:i + size] + [self.sep] for i in range(0, max(len(ids), 1), size)]

    def _encode(self, seqs, batch=32):
        out = []
        for b in range(0, len(seqs), batch):
            chunk = seqs[b:b + batch]
            width = max(len(s) for s in chunk)
            ids = np.zeros((len(chunk), width), dtype=np.int64)
            mask = np.zeros_like(ids)
            for i, s in enumerate(chunk):
                ids[i, :len(s)], mask[i, :len(s)] = s, 1
            hidden = self.sess.run(None, {"input_ids": ids, "attention_mask": mask, "token_type_ids": np.zeros_like(ids)})[0]
            out.append(hidden[:, 0, :])  # CLS pooling
        return normalize(np.concatenate(out))

    def embed_passages(self, texts):
        """→ (float32 [n, 384] unit vectors, pieces per text). Multi-piece texts: mean of piece vectors, renormalized."""
        per_text = [self.pieces(t) for t in texts]
        flat = self._encode([s for ps in per_text for s in ps])
        vecs, i = [], 0
        for ps in per_text:
            vecs.append(flat[i:i + len(ps)].mean(axis=0))
            i += len(ps)
        return normalize(np.stack(vecs)).astype(np.float32), [len(ps) for ps in per_text]

    def embed_queries(self, queries):
        seqs = [self.pieces(QUERY_INSTRUCTION + q) for q in queries]
        assert all(len(s) == 1 for s in seqs), "query longer than the model window"
        return self._encode([s[0] for s in seqs]).astype(np.float32)


def normalize(x):
    return x / np.linalg.norm(x, axis=1, keepdims=True)


def load_index():
    """(chunks, embeddings); fails loudly if rows and chunks are out of step."""
    chunks = [json.loads(line) for line in (INDEX / "chunks.jsonl").read_text().splitlines()]
    emb = np.load(INDEX / "embeddings.npy")
    ids = json.loads((INDEX / "embeddings_ids.json").read_text())
    assert ids == [c["chunk_id"] for c in chunks] and len(emb) == len(chunks), "embeddings out of sync with chunks.jsonl"
    return chunks, emb


def main():
    chunks = [json.loads(line) for line in (INDEX / "chunks.jsonl").read_text().splitlines()]
    emb, pieces = Embedder().embed_passages([c["text"] for c in chunks])
    np.save(INDEX / "embeddings.npy", emb)
    (INDEX / "embeddings_ids.json").write_text(json.dumps([c["chunk_id"] for c in chunks]))
    for size in sorted({c["size"] for c in chunks}):
        p = [n for c, n in zip(chunks, pieces) if c["size"] == size]
        print(f"size {size}: {len(p)} chunks, {sum(n > 1 for n in p)} exceed {WINDOW} model tokens, "
              f"split into {sum(n for n in p if n > 1)} pieces (max {max(p)} per chunk)")


if __name__ == "__main__":
    main()
