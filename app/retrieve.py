"""Retrieval primitives shared by the Phase 3 evaluation and the pipeline: BM25, RRF, rule-based router."""
import math
import re
import unicodedata
from collections import Counter

import numpy as np


def tokenize(text):
    """NFKC, lowercase, alphanumeric tokens with decimals kept whole ("28.4", "4.2m").
    Hyphenated words index their parts AND the joined form (cleanup rule 10): pdftotext writes
    "position-wise" as "positionwise", so "position-wise" → position, wise, positionwise matches both."""
    out = []
    for word in re.findall(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*", unicodedata.normalize("NFKC", text).lower()):
        parts = word.split("-")
        out += parts
        if len(parts) > 1 and all(re.search(r"[a-z]", p) for p in parts):  # no joined form for "4.5-5"
            out.append("".join(parts))
    return out


class BM25:
    """Okapi BM25, k1=1.5, b=0.75 (fixed, no tuning). IDF = ln(1 + (N - df + 0.5) / (df + 0.5)), never negative."""

    def __init__(self, texts, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        docs = [Counter(tokenize(t)) for t in texts]
        self.len = np.array([sum(d.values()) for d in docs], dtype=float)
        self.avgdl = self.len.mean()
        self.postings = {}
        for i, d in enumerate(docs):
            for term, tf in d.items():
                self.postings.setdefault(term, []).append((i, tf))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - len(p) + 0.5) / (len(p) + 0.5)) for t, p in self.postings.items()}

    def scores(self, query):
        s = np.zeros(len(self.len))
        for term in tokenize(query):
            for i, tf in self.postings.get(term, ()):
                s[i] += self.idf[term] * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * self.len[i] / self.avgdl))
        return s


def rank(scores):
    """Indices best-first; ties broken by index so results are deterministic."""
    return np.lexsort((np.arange(len(scores)), -np.asarray(scores)))


def rrf(*rankings, k=60):
    """Reciprocal rank fusion over FULL rankings (lists of indices, best first) → fused ranking."""
    fused = np.zeros(len(rankings[0]))
    for r in rankings:
        fused[np.asarray(r)] += 1.0 / (k + np.arange(1, len(r) + 1))
    return rank(fused)


ROUTES = {"moshi": r"\bmoshi\b", "dynchunk": r"\bh-net\b|\bdynamic chunking\b", "attention": r"attention is all you need"}


def route(question):
    """Papers explicitly named in the question, or None (search all). Names only: 'Transformer'/'attention' appear
    in all three papers, so they never route."""
    named = {p for p, pat in ROUTES.items() if re.search(pat, question, re.I)}
    return named or None
