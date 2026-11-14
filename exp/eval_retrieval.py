"""Phase 3: retrieval-only evaluation. Zero Groq calls.

  python exp/eval_retrieval.py   →  prints tables; writes results/retrieval_eval.csv, results/gate_scores.csv

Hit rule (NOTES.md): chunk from the stated paper, page_start <= page <= page_end, contains the key phrase or an
alt phrase (whitespace-insensitive after NFKC). "Strict" hit: the phrase is in the part of the chunk that came
from the stated page itself (exact per-word page attribution, rebuilt with the chunker's own page_words()).
"""
import csv
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "exp")]
import embed  # noqa: E402
import ingest  # noqa: E402
import retrieve  # noqa: E402
from validate_questions import contains  # noqa: E402

SIZES = (200, 400, 800)
BUDGET = {200: 8, 400: 4, 800: 2}  # ~1,600 context tokens each
RESULTS = ROOT / "results"


def pct(a, n):
    return f"{a}/{n} ({100 * a / n:.1f}%)" if n else "–"


class Attribution:
    """chunk → text from one page only, by locating the chunk's words in the paper's (word, page) stream."""

    def __init__(self):
        self.words = {}
        for arxiv in ingest.PAPERS:
            pages = ingest.load_clean(arxiv)
            self.words[ingest.SHORT[arxiv]] = ingest.page_words({p: t for p, t in pages.items() if t.strip()})

    def page_text(self, chunk, page):
        ws, cw = self.words[chunk["paper"]], chunk["text"].split()
        for i, (w, p, _) in enumerate(ws):
            if p == chunk["page_start"] and w == cw[0] and [x[0] for x in ws[i:i + len(cw)]] == cw:
                return " ".join(w for w, p, _ in ws[i:i + len(cw)] if p == page)
        raise AssertionError(f"{chunk['chunk_id']}: words not found in clean text (index out of sync)")


def phrases(q):
    return [q["key_phrase"], *q.get("alt_phrases", [])]


def is_hit(chunk, q):
    return (chunk["paper"] == q["paper"] and chunk["page_start"] <= q["page"] <= chunk["page_end"]
            and any(contains(chunk["text"], ph) for ph in phrases(q)))


def first_rank(ranking, targets):
    for r, idx in enumerate(ranking, 1):
        if idx in targets:
            return r
    return None


def main():
    chunks, emb = embed.load_index()
    qs = json.loads((ROOT / "eval" / "questions.json").read_text())
    rq = qs["retrieval_questions"]
    gate_qs = ([(q["id"], q["question"], "answerable", "retrieval") for q in rq]
               + [(q["id"], q["question"], q["subtype"], "answer") for q in qs["answer_questions"]])
    att, emb_model = Attribution(), embed.Embedder()
    qvec = dict(zip([g[0] for g in gate_qs], emb_model.embed_queries([g[1] for g in gate_qs])))

    rows, gate_rows, rankings = [], [], {}
    for size in SIZES:
        idx = np.array([i for i, c in enumerate(chunks) if c["size"] == size])
        sub = [chunks[i] for i in idx]
        bm25 = retrieve.BM25([c["text"] for c in sub])
        best_piece = None
        if size == 800:  # ablation: score a chunk by its best-matching piece instead of the averaged vector
            per = [emb_model.pieces(c["text"]) for c in sub]
            flat = emb_model._encode([s for ps in per for s in ps])
            bounds = np.cumsum([0] + [len(ps) for ps in per])
            best_piece = lambda qv: np.array([(flat[a:b] @ qv).max() for a, b in zip(bounds, bounds[1:])])
        hits = {q["id"]: {j for j, c in enumerate(sub) if is_hit(c, q)} for q in rq}
        strict = {q["id"]: {j for j in hits[q["id"]] if any(contains(att.page_text(sub[j], q["page"]), ph) for ph in phrases(q))}
                  for q in rq}

        def ranks(qid, text, variant):
            b, s = bm25.scores(text), emb[idx] @ qvec[qid]
            if variant == "bestpiece":
                s = best_piece(qvec[qid])
            out = {"bm25": (retrieve.rank(b), b), "semantic": (retrieve.rank(s), s)}
            out["hybrid"] = (retrieve.rrf(out["bm25"][0], out["semantic"][0]), s)  # hybrid gate signal = semantic cosine
            if variant == "router" and (papers := retrieve.route(text)):
                out = {k: (np.array([j for j in r if sub[j]["paper"] in papers]), sc) for k, (r, sc) in out.items()}
            return out

        for variant in ("base", "router") + (("bestpiece",) if size == 800 else ()):
            for q in rq:
                for ret, (r, sc) in ranks(q["id"], q["question"], variant).items():
                    if variant == "bestpiece" and ret == "bm25":
                        continue
                    fr, fs = first_rank(r, hits[q["id"]]), first_rank(r, strict[q["id"]])
                    k = BUDGET[size]
                    rows.append({"qid": q["id"], "paper": q["paper"], "type": q["type"], "retriever": ret, "size": size,
                                 "variant": variant, "routed": ",".join(sorted(retrieve.route(q["question"]) or [])) if variant == "router" else "",
                                 "first_hit_rank": fr or "", "first_strict_hit_rank": fs or "",
                                 "hit_at_5": int(bool(fr and fr <= 5)), "hit_at_10": int(bool(fr and fr <= 10)),
                                 "budget_k": k, "hit_at_budget": int(bool(fr and fr <= k)),
                                 "strict_hit_at_budget": int(bool(fs and fs <= k)),
                                 "top1_chunk": sub[r[0]]["chunk_id"], "top1_score": round(float(sc[r[0]]), 6),
                                 "hit_chunk": sub[r[fr - 1]]["chunk_id"] if fr else ""})
                    if variant == "base":
                        rankings[(ret, size, q["id"])] = [(sub[j], float(sc[j])) for j in r[:3]]
        for qid, text, group, source in gate_qs:
            for ret, (r, sc) in ranks(qid, text, "base").items():
                if ret != "hybrid":
                    gate_rows.append({"qid": qid, "group": group, "source": source, "retriever": ret, "size": size,
                                      "top1_score": round(float(sc[r[0]]), 6), "top1_chunk": sub[r[0]]["chunk_id"]})
        n_hit = sum(len(v) for v in hits.values())
        n_strict = sum(len(v) for v in strict.values())
        print(f"size {size}: hit-rule chunk matches (all chunks, not just retrieved): {n_hit}, of which phrase on the "
              f"stated page itself: {n_strict}, span-only: {n_hit - n_strict}")

    RESULTS.mkdir(exist_ok=True)
    for path, data in ((RESULTS / "retrieval_eval.csv", rows), (RESULTS / "gate_scores.csv", gate_rows)):
        with path.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(data[0]))
            w.writeheader()
            w.writerows(data)
    report(rows, gate_rows, rq, rankings)


def report(rows, gate_rows, rq, rankings):
    rets, metrics = ("bm25", "semantic", "hybrid"), (("hit_at_5", "R@5"), ("hit_at_10", "R@10"), ("hit_at_budget", "R@budget"))
    sel = lambda variant, ret, size, **f: [r for r in rows if r["variant"] == variant and r["retriever"] == ret and r["size"] == size
                                          and all(r[k] == v for k, v in f.items())]

    def table(title, variant, groups=None, key=None, sizes=SIZES, which=rets):
        print(f"\n### {title}")
        for col, name in metrics if groups is None else metrics:
            if groups is None:
                if col != "hit_at_5":
                    continue
                print(f"{'setting':<18}" + "".join(f"{n:<20}" for _, n in metrics))
                for ret in which:
                    for size in sizes:
                        rs = sel(variant, ret, size)
                        print(f"{ret + ' ' + str(size):<18}" + "".join(f"{pct(sum(r[c] for r in rs), len(rs)):<20}" for c, _ in metrics))
            else:
                print(f"\n{name}:\n{'setting':<18}" + "".join(f"{g + ' (n=' + str(sum(q[key] == g for q in rq)) + ')':<20}" for g in groups))
                for ret in which:
                    for size in sizes:
                        print(f"{ret + ' ' + str(size):<18}" + "".join(
                            f"{pct(sum(r[col] for r in sel(variant, ret, size, **{key: g})), len(sel(variant, ret, size, **{key: g}))):<20}"
                            for g in groups))

    table("Overall (n=40), base: all papers, no router", "base")
    table("Per paper, base", "base", ("attention", "moshi", "dynchunk"), "paper")
    table("Per question type, base", "base", ("fact", "method", "table", "paraphrase", "exact-term", "cross-section"), "type")

    print("\n### Span-only hits at @budget (counted as hit, but phrase NOT on the stated page's own text)")
    print(f"{'setting':<18}{'hits@budget':<14}{'strict@budget':<15}span-only")
    for ret in rets:
        for size in SIZES:
            rs = sel("base", ret, size)
            h, s = sum(r["hit_at_budget"] for r in rs), sum(r["strict_hit_at_budget"] for r in rs)
            print(f"{ret + ' ' + str(size):<18}{h:<14}{s:<15}{h - s}")

    print("\n### 800-token averaging confound: averaged vectors vs best-matching piece (semantic, hybrid)")
    print(f"{'setting':<26}" + "".join(f"{n:<20}" for _, n in metrics))
    for ret in ("semantic", "hybrid"):
        for variant in ("base", "bestpiece"):
            rs = sel(variant, ret, 800)
            print(f"{ret + ' 800 ' + ('averaged' if variant == 'base' else 'best-piece'):<26}"
                  + "".join(f"{pct(sum(r[c] for r in rs), len(rs)):<20}" for c, _ in metrics))

    routed = sorted({r["qid"] for r in rows if r["variant"] == "router" and r["routed"]})
    print(f"\n### ROUTER ROW (separate; rule: route only on explicit paper names). Routed questions: {len(routed)}/40 {routed}")
    table("With router, overall (n=40)", "router")
    wrong = sorted({(r["qid"], r["routed"], r["paper"]) for r in rows if r["variant"] == "router" and r["routed"] and r["paper"] not in r["routed"].split(",")})
    print(f"routed to a paper that is not the gold paper: {wrong or 'none'}")

    print("\n### Top-1 score distributions and gate (INDICATIVE: n=5 off-topic, n=5 near-miss). Answer if score >= threshold.")
    print("threshold = lowest answerable top-1 (refuses no answerable question by construction). Hybrid uses the semantic signal.")
    for ret in ("bm25", "semantic"):
        print(f"\n{ret}:")
        print(f"{'size':<6}{'answerable (n=47) min/med/max':<32}{'off-topic (sorted)':<36}{'near-miss (sorted)':<36}"
              f"{'threshold':<11}{'false-refusal':<15}{'false-answer off':<18}{'false-answer near':<19}{'margin: min ans - max off'}")
        for size in SIZES:
            g = {grp: sorted(r["top1_score"] for r in gate_rows if r["retriever"] == ret and r["size"] == size and r["group"] == grp)
                 for grp in ("answerable", "off-topic", "near-miss")}
            a, off, near = g["answerable"], g["off-topic"], g["near-miss"]
            thr = a[0]
            fmt = lambda xs: " ".join(f"{x:.3f}" if ret == "semantic" else f"{x:.1f}" for x in xs)
            print(f"{size:<6}{fmt([a[0], float(np.median(a)), a[-1]]):<32}{fmt(off):<36}{fmt(near):<36}{thr:<11.3f}"
                  f"{pct(sum(x < thr for x in a), len(a)):<15}{pct(sum(x >= thr for x in off), len(off)):<18}"
                  f"{pct(sum(x >= thr for x in near), len(near)):<19}{a[0] - off[-1]:+.3f} (lowest answerable {a[0]:.3f}, highest off-topic {off[-1]:.3f})")

    print("\n### Missed questions at the best Recall@budget setting per retriever (tie-break: R@10, then smaller size)")
    for ret in rets:
        best = max(SIZES, key=lambda s: (sum(r["hit_at_budget"] for r in sel("base", ret, s)),
                                         sum(r["hit_at_10"] for r in sel("base", ret, s)), -s))
        missed = [r for r in sel("base", ret, best) if not r["hit_at_budget"]]
        print(f"\n{ret} {best} (k={BUDGET[best]}): {len(missed)} missed")
        qmap = {q["id"]: q for q in rq}
        for r in missed:
            q = qmap[r["qid"]]
            print(f"  {q['id']} [{q['type']}] gold {q['paper']} p{q['page']} (first hit rank {r['first_hit_rank'] or 'none'}): {q['question']}")
            for c, s in rankings[(ret, best, q["id"])]:
                pages = f"p{c['page_start']}" + (f"–{c['page_end']}" if c["page_end"] != c["page_start"] else "")
                print(f"      {s:8.3f}  {c['paper']:<9}{pages:<8}{c['text'][:85]!r}")


if __name__ == "__main__":
    main()
