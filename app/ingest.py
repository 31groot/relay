"""Extract and clean the three papers, one text file per ORIGINAL page number.

  python app/ingest.py   →  data/raw/<paper>/pNNN.txt   (pdftotext output actually used)
                            data/clean/<paper>/pNNN.txt (after cleanup; pages that end up empty are not written)

Cleanup rules (numbers match NOTES / Phase 2.1 table):
  1 arXiv stamp · 2 running title · 3 page-number footer · 4 Google permission notice
  5 references cut at heading, appendices kept · 6 figure text · 7 NFKC · 8 section-number joining
Tables: pages with a "Table N:" caption are extracted with `pdftotext -layout` to keep rows aligned.
"""
import re
import subprocess
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAPERS = ["1706.03762v7", "2410.00037v1", "2507.07955v2"]

# Rule 5: first line AFTER the references that starts real content again (found by inspection, Phase 2.1).
REFS_END = {
    "1706.03762v7": r"^Attention Visualizations$",
    "2410.00037v1": r"^Appendix A\.",
    "2507.07955v2": r"^A$",  # bare appendix letter; next line is "Related Work"
}


def extract(pdf, page, layout=False):
    cmd = ["pdftotext", "-f", str(page), "-l", str(page), "-enc", "UTF-8"] + (["-layout"] if layout else []) + [str(pdf), "-"]
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def page_count(pdf):
    out = subprocess.run(["pdfinfo", str(pdf)], check=True, capture_output=True, text=True).stdout
    return int(re.search(r"^Pages:\s+(\d+)", out, re.M).group(1))


def is_table_page(text):
    return re.search(r"^\s*Table \d+:", text, re.M) is not None


# ---- rules: each takes and returns one page's text ----

def nfkc(text):  # rule 7: 𝑥 → x, ligatures → letters
    return unicodedata.normalize("NFKC", text)


def strip_arxiv_stamp(text):  # rule 1
    return re.sub(r"^\s*arXiv:\d{4}\.\d{4,5}v\d+\s+\[[^\]]+\]\s+\d{1,2} \w{3} \d{4}\s*\n?", "", text, flags=re.M)


def strip_permission_notice(text):  # rule 4 (Attention p1)
    return re.sub(r"Provided proper attribution is provided, Google hereby grants permission to.*?scholarly works\.\s*",
                  "", text, flags=re.S)


def first_line(text):
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def detect_running_title(pages):
    """A first line that repeats on more than half the pages is a running title (Moshi: 65/66)."""
    line, n = Counter(first_line(t) for t in pages.values()).most_common(1)[0]
    return line if line and n > len(pages) / 2 else None


def strip_running_title(text, title):  # rule 2: only the first non-empty line
    if not title:
        return text
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip():
            return "".join(lines[:i] + lines[i + 1:]) if line.strip() == title else text
    return text


def strip_page_number(text, page):  # rule 3: last non-empty line is exactly the page number
    lines = text.rstrip().splitlines()
    if lines and lines[-1].strip() == str(page):
        lines = lines[:-1]
    return "\n".join(lines) + "\n"


def is_figure_junk(line):
    # single-token lines (attention-visualization word lists) or short wordless lines (plot ticks)
    toks = line.split()
    return len(toks) <= 1 or (len(toks) <= 3 and not re.search(r"[A-Za-z]{4,}", line))


def strip_figure_text(text, min_run=5):  # rule 6: runs of ≥5 short, wordless lines (ticks, word lists)
    lines = text.splitlines()
    nonblank = [i for i, line in enumerate(lines) if line.strip()]
    drop, run = set(), []
    for i in nonblank + [None]:
        if i is not None and is_figure_junk(lines[i]):
            run.append(i)
            continue
        if len(run) >= min_run:
            drop.update(range(run[0], run[-1] + 1))  # blank lines inside the run go too
        run = []
    return "\n".join(line for i, line in enumerate(lines) if i not in drop) + "\n"


def join_section_numbers(text):  # rule 8: "3.1" ⏎ (blank) ⏎ "Encoder and Decoder Stacks" → one line
    # parts are 1-2 digits without a leading zero, so decimals like "1.00" / "0.95" (plot ticks) never match
    return re.sub(r"^[ \t]*((?:[1-9]\d?|[A-Z])(?:\.[1-9]\d?)*)[ \t]*\n(?:[ \t]*\n)*[ \t]*([A-Z][A-Za-z][^\n]{0,80})$",
                  r"\1 \2", text, flags=re.M)


def cut_references(pages, paper):  # rule 5, across pages; keeps text before the heading and from REFS_END on
    start = next(((p, m.start()) for p, t in sorted(pages.items())
                  for m in [re.search(r"^\s*(References|Bibliography)\s*$", t, re.M)] if m), None)
    if start is None:
        raise ValueError(f"{paper}: no References heading")
    end = next(((p, m.start()) for p, t in sorted(pages.items()) if p >= start[0]
                for m in [re.search(REFS_END[paper], t[start[1]:] if p == start[0] else t, re.M)] if m), None)
    if end is None:
        raise ValueError(f"{paper}: references end marker {REFS_END[paper]!r} not found")
    end_off = end[1] + (start[1] if end[0] == start[0] else 0)
    out = {}
    for p, t in pages.items():
        if p < start[0] or p > end[0]:
            out[p] = t
        elif p == start[0] == end[0]:
            out[p] = t[:start[1]] + t[end_off:]
        elif p == start[0]:
            out[p] = t[:start[1]]
        elif p == end[0]:
            out[p] = t[end_off:]
        else:
            out[p] = ""
    return out


def clean(paper, raw, layout_pages=()):
    """raw: {page_no: text} → {page_no: cleaned text}; page numbers are never renumbered."""
    pages = {p: strip_permission_notice(strip_arxiv_stamp(nfkc(t))) for p, t in raw.items()}
    title = detect_running_title(pages)
    pages = {p: strip_page_number(strip_running_title(t, title), p) for p, t in pages.items()}
    pages = cut_references(pages, paper)
    # rule 6 skips table pages: sparse table rows ("✓ 0.8 98.40", Attention Table 3) look like figure text.
    # shortcut: plot ticks on mixed plot+table pages (e.g. DC p13) survive; split by region if they hurt retrieval.
    return {p: join_section_numbers(t if p in layout_pages else strip_figure_text(t)) for p, t in pages.items()}


def load_raw(paper):
    pdf = ROOT / "data" / "papers" / f"{paper}.pdf"
    raw, layout_pages = {}, []
    for p in range(1, page_count(pdf) + 1):
        text = extract(pdf, p)
        if is_table_page(text):
            text = extract(pdf, p, layout=True)
            layout_pages.append(p)
        raw[p] = text
    return raw, layout_pages


def main():
    for paper in PAPERS:
        raw, layout_pages = load_raw(paper)
        cleaned = clean(paper, raw, layout_pages)
        for kind, pages in (("raw", raw), ("clean", cleaned)):
            d = ROOT / "data" / kind / paper
            d.mkdir(parents=True, exist_ok=True)
            for old in d.glob("p*.txt"):
                old.unlink()
            for p, t in pages.items():
                if t.strip():
                    (d / f"p{p:03d}.txt").write_text(t)
        words = lambda pages: sum(len(t.split()) for t in pages.values())
        kept = [p for p, t in cleaned.items() if t.strip()]
        print(f"{paper}: {len(raw)} pages, layout pages {layout_pages}, "
              f"words {words(raw)} → {words(cleaned)}, non-empty clean pages {len(kept)}")


if __name__ == "__main__":
    main()
