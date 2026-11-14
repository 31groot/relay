"""Phase 5 automatic first-pass grading (the user's grade is final; overrides are logged separately).

Answerable (expected "answer"): correct  = released AND every must_include term present AND a cited chunk covers
                                            evidence_page of the stated paper
                                 wrong    = released otherwise;  refused_wrongly = refused
Unanswerable (expected "refuse"): refused_correctly = refused (gate, NOT_IN_PAPERS, failed correction); wrong = released
A provider error is graded "error" (not a refusal) and flagged.
Term matching: NFKC, case-folded, every dash (Unicode Pd) → "-", all whitespace removed, thousands separators removed
("4,096" == "4096").
"""
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_questions import contains  # noqa: E402

LABELS = re.compile(r"\[\s*S\d+(?:\s*,\s*S\d+)*\s*\]")


def norm_terms(s):
    s = unicodedata.normalize("NFKC", s).casefold()
    s = "".join("-" if unicodedata.category(ch) == "Pd" else ch for ch in s)  # U+2011 → U+2010 under NFKC, not "-"
    s = re.sub(r"(?<=\d)[,   ](?=\d{3}\b)", "", s)   # 4,096 / 4 096 → 4096
    return re.sub(r"\s+", "", s)


def has_term(answer, term):
    return norm_terms(term) in norm_terms(answer)


def answer_text(res):
    """Normalized text of the released attempt, citation labels removed (so labels never satisfy a term)."""
    return LABELS.sub(" ", res["attempts"][-1].get("normalized_output", "")) if res["attempts"] else ""


def cited_chunks(res, chunks_by_id):
    a = res["attempts"][-1] if res["attempts"] else {}
    return [chunks_by_id[res["retrieved"][int(lab[1:]) - 1]["chunk_id"]] for lab in a.get("cited_labels", [])]


def covers(chunk, paper, page):
    return chunk["paper"] == paper and chunk["page_start"] <= page <= chunk["page_end"]


def evidence_in_context(q, res, chunks_by_id):
    """Gold evidence phrase on the evidence page present in ANY retrieved chunk (answerable questions only)."""
    if q["expected"] != "answer":
        return None
    return any(covers(c, q["paper"], q["evidence_page"]) and contains(c["text"], q["evidence_phrase"])
               for c in (chunks_by_id[r["chunk_id"]] for r in res["retrieved"]))


def grade(q, res, chunks_by_id):
    out = {"evidence_in_context": evidence_in_context(q, res, chunks_by_id), "missing_terms": [], "cites_evidence_page": None}
    if res["outcome"] == "error":
        return {**out, "auto_grade": "error"}
    released = res["outcome"] == "answered"
    if q["expected"] == "refuse":
        return {**out, "auto_grade": "wrong" if released else "refused_correctly"}
    if not released:
        return {**out, "auto_grade": "refused_wrongly"}
    text = answer_text(res)
    out["missing_terms"] = [t for t in q["must_include"] if not has_term(text, t)]
    out["cites_evidence_page"] = any(covers(c, q["paper"], q["evidence_page"]) for c in cited_chunks(res, chunks_by_id))
    ok = not out["missing_terms"] and out["cites_evidence_page"]
    return {**out, "auto_grade": "correct" if ok else "wrong"}
