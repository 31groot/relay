"""Validate questions.json format against data/clean/ (and data/raw/ for diagnosis). Read-only.

  python exp/validate_questions.py PATH/TO/questions.json

Matching (user decision, Phase 2.2): NFKC (same as ingest), then ALL whitespace removed on both sides.
pdftotext spaces subscripts unpredictably ("bt = 1" vs "bt= 1"), so spacing must not decide a match.
"""
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAPER_IDS = {"attention": "1706.03762v7", "moshi": "2410.00037v1", "dynchunk": "2507.07955v2"}
PAGES = {"attention": 15, "moshi": 66, "dynchunk": 39}

RETRIEVAL_FIELDS = {"id", "paper", "type", "question", "page", "key_phrase"}
ANSWER_FIELDS = {"id", "expected", "subtype", "paper", "question"}
ANSWERABLE_FIELDS = {"reference_answer", "must_include", "evidence_page", "evidence_phrase"}


def nospace(s):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s))


def contains(text, phrase):
    return nospace(phrase) in nospace(text)


def load_pages(kind, paper):
    d = ROOT / "data" / kind / PAPER_IDS[paper]
    return {int(f.stem[1:]): f.read_text() for f in d.glob("p*.txt")}


def locate(phrase, paper, page, texts):
    """→ (status, detail). status: ok | elsewhere | raw-only | missing."""
    clean, raw = texts[("clean", paper)], texts[("raw", paper)]
    if contains(clean.get(page, ""), phrase):
        return "ok", ""
    other = sorted(n for n, t in clean.items() if contains(t, phrase))
    if other:
        return "elsewhere", f"found on clean page(s) {other}"
    raw_hit = sorted(n for n, t in raw.items() if contains(t, phrase))
    if raw_hit:
        return "raw-only", f"absent from clean text, present in RAW page(s) {raw_hit} → a cleanup rule removed it"
    return "missing", "not found in clean or raw text of this paper"


def validate(qs):
    texts = {(k, p): load_pages(k, p) for k in ("clean", "raw") for p in PAGES}
    errors, phrase_rows = [], []

    def check_page(qid, paper, n, field):
        if paper not in PAGES:
            errors.append(f"{qid}: unknown paper {paper!r}")
        elif not isinstance(n, int) or not 1 <= n <= PAGES[paper]:
            errors.append(f"{qid}: {field}={n!r} outside {paper} range 1–{PAGES[paper]}")
        else:
            return True
        return False

    def check_phrase(qid, paper, page, phrase, field, pages=None):
        for pg in pages or [page]:
            status, detail = locate(phrase, paper, pg, texts)
            if status == "ok":
                phrase_rows.append((qid, field, page, "ok", ""))
                return
        phrase_rows.append((qid, field, page if not pages else pages, status, detail))

    rq, aq = qs.get("retrieval_questions", []), qs.get("answer_questions", [])
    ids = [q.get("id") for q in rq + aq]
    for qid, n in Counter(ids).items():
        if n > 1:
            errors.append(f"{qid}: duplicate id ({n}×)")

    for q in rq:
        qid = q.get("id", "?")
        if missing := RETRIEVAL_FIELDS - q.keys():
            errors.append(f"{qid}: missing fields {sorted(missing)}")
            continue
        if not check_page(qid, q["paper"], q["page"], "page"):
            continue
        pages = q.get("source_pages")
        if pages is not None and not all(check_page(qid, q["paper"], n, "source_pages") for n in pages):
            continue
        check_phrase(qid, q["paper"], q["page"], q["key_phrase"], "key_phrase")
        for alt in q.get("alt_phrases", []):
            check_phrase(qid, q["paper"], q["page"], alt, "alt_phrase", sorted({q["page"], *(pages or [])}))

    for q in aq:
        qid = q.get("id", "?")
        if missing := ANSWER_FIELDS - q.keys():
            errors.append(f"{qid}: missing fields {sorted(missing)}")
            continue
        if q["expected"] == "answer":
            if missing := ANSWERABLE_FIELDS - q.keys():
                errors.append(f"{qid}: answerable but missing {sorted(missing)}")
                continue
            if check_page(qid, q["paper"], q["evidence_page"], "evidence_page"):
                check_phrase(qid, q["paper"], q["evidence_page"], q["evidence_phrase"], "evidence_phrase")
        elif q["expected"] == "refuse":
            if "why_unanswerable" not in q:
                errors.append(f"{qid}: refusal without why_unanswerable")
            if q["subtype"] == "near-miss":
                if "near_miss_page" not in q:
                    errors.append(f"{qid}: near-miss without near_miss_page")
                elif check_page(qid, q["paper"], q["near_miss_page"], "near_miss_page"):
                    if not texts[("clean", q["paper"])].get(q["near_miss_page"], "").strip():
                        errors.append(f"{qid}: near_miss_page {q['near_miss_page']} has no clean text")
            elif q["paper"] != "all":
                errors.append(f"{qid}: off-topic refusal with paper {q['paper']!r} (expected 'all')")
        else:
            errors.append(f"{qid}: expected={q['expected']!r} (want 'answer' or 'refuse')")
    return errors, phrase_rows


def main(path):
    qs = json.loads(Path(path).read_text())
    rq, aq = qs.get("retrieval_questions", []), qs.get("answer_questions", [])
    errors, rows = validate(qs)

    print(f"Retrieval questions: {len(rq)}  by paper {dict(Counter(q.get('paper') for q in rq))}  by type {dict(Counter(q.get('type') for q in rq))}")
    print(f"Answer questions:    {len(aq)}  by subtype {dict(Counter(q.get('subtype') for q in aq))}  by paper {dict(Counter(q.get('paper') for q in aq))}")
    meta = qs.get("metadata", {})
    claims = {"retrieval_count": len(rq), "answer_count": len(aq)}
    for k, actual in claims.items():
        if k in meta and meta[k] != actual:
            errors.append(f"metadata.{k}={meta[k]} but file has {actual}")
    for k, v in meta.get("retrieval_counts_by_paper", {}).items():
        if v != sum(q.get("paper") == k for q in rq):
            errors.append(f"metadata.retrieval_counts_by_paper.{k}={v} but file has {sum(q.get('paper') == k for q in rq)}")
    for k, v in meta.get("answer_counts", {}).items():
        if v != sum(q.get("subtype") == k for q in aq):
            errors.append(f"metadata.answer_counts.{k}={v} but file has {sum(q.get('subtype') == k for q in aq)}")

    ok = sum(r[3] == "ok" for r in rows)
    print(f"\nPhrase checks: {ok}/{len(rows)} ok")
    print("\nFAILURES" if errors or ok < len(rows) else "\nNo failures.")
    for e in errors:
        print(f"  [format] {e}")
    for qid, field, page, status, detail in rows:
        if status != "ok":
            print(f"  [{status}] {qid} {field} (stated page {page}): {detail}")
    return 0 if not errors and ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
