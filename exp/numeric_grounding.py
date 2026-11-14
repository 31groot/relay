"""Phase 5 post-hoc analysis (NOT part of the pipeline): numeric grounding.

Rule: every number in the answer (citation labels excluded), after NFKC and thousands-separator removal, must appear
as a whole number (not inside a longer number) in the text of the chunks the answer cites.

  python exp/numeric_grounding.py   → report over the 27 corrupted items and the released default/baseline answers.
Label: post-hoc, same items, not independent (the rule was designed after seeing these misses).
"""
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "exp"), str(ROOT / "app")]

LABELS = re.compile(r"\[\s*S\d+(?:\s*,\s*S\d+)*\s*\]")
NUM = re.compile(r"(?<![\d.])\d+(?:\.\d+)?(?![\d]|\.\d)")


def _norm(s):
    s = unicodedata.normalize("NFKC", s)
    return re.sub(r"(?<=\d)[,   ](?=\d{3}\b)", "", s)   # 4,096 → 4096


def numbers(text):
    return NUM.findall(_norm(LABELS.sub(" ", text)))


def ungrounded_numbers(answer, cited_texts):
    """Numbers in the answer that do not occur as whole numbers in any cited chunk."""
    pool = set(NUM.findall(_norm(" ".join(cited_texts))))
    return [n for n in numbers(answer) if n not in pool]


def cited_texts(text, retrieved_ids, chunks_by_id):
    import pipeline as pl
    return [chunks_by_id[retrieved_ids[n - 1]]["text"] for n in sorted(set(pl.cited_labels(text))) if 1 <= n <= len(retrieved_ids)]


def main():
    import run_corruption as rc
    import run_phase5 as rp
    qs = {q["id"]: q for q in json.loads((ROOT / "eval" / "questions.json").read_text())["answer_questions"]}
    chunks = {c["chunk_id"]: c for c in map(json.loads, (ROOT / "data" / "index" / "chunks.jsonl").read_text().splitlines())}
    judged = rc.done_items(rp.LOG)
    print("POST-HOC, SAME ITEMS, NOT INDEPENDENT\n\nCorrupted items (27 judged):")
    print(f"{'type':<22}{'flagged by numeric rule':<26}{'judge caught':<14}{'caught by either'}")
    for kind in rc.KINDS:
        items = [i for i in rc.build_items(rp.LOG, qs, chunks) if i["kind"] == kind and i["text"]]
        flag = [bool(ungrounded_numbers(i["text"], cited_texts(i["text"], i["retrieved"], chunks))) for i in items]
        judge = [judged[i["item_id"]]["verdict"] == "unsupported" for i in items]
        n = len(items)
        print(f"{kind:<22}{f'{sum(flag)}/{n}':<26}{f'{sum(judge)}/{n}':<14}{sum(a or b for a, b in zip(flag, judge))}/{n}"
              + ("   (controls: flags here are FALSE rejections)" if kind == "control" else ""))
        for i, f, j in zip(items, flag, judge):
            if kind != "control" and not j:
                print(f"    judge missed {i['item_id']}: numeric rule {'FLAGS' if f else 'does NOT flag'} "
                      f"{ungrounded_numbers(i['text'], cited_texts(i['text'], i['retrieved'], chunks))}")
    print("\nReleased answers (latest rows, outcome answered):")
    latest = rp.latest_requests(rp.LOG)
    for setting in ("default", "baseline"):
        released = [(rid, r) for rid, r in sorted(latest.items()) if rid.startswith(setting + ":") and r["outcome"] == "answered"]
        bad = []
        for rid, r in released:
            text = r["attempts"][-1]["normalized_output"]
            u = ungrounded_numbers(text, cited_texts(text, [x["chunk_id"] for x in r["retrieved"]], chunks))
            if u:
                bad.append((rid, u, text))
        print(f"  {setting}: would wrongly reject {len(bad)}/{len(released)}")
        for rid, u, text in bad:
            print(f"    FALSE REJECTION {rid}: ungrounded {u}\n      answer: {text}")


if __name__ == "__main__":
    main()
