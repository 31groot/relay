"""Each cleanup rule tested on real pages extracted from the committed PDFs (needs poppler's pdftotext)."""
import sys
from functools import lru_cache
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import ingest  # noqa: E402

ATT, MOSHI, DC = ingest.PAPERS


@lru_cache(maxsize=None)
def loaded(paper):
    return ingest.load_raw(paper)


def raw(paper):
    return loaded(paper)[0]


@lru_cache(maxsize=None)
def cleaned(paper):
    return ingest.clean(paper, *loaded(paper))


def test_rule1_arxiv_stamp():
    before = ingest.nfkc(raw(ATT)[1])
    assert "arXiv:1706.03762v7 [cs.CL] 2 Aug 2023" in before
    after = ingest.strip_arxiv_stamp(before)
    assert "arXiv:1706.03762v7" not in after and "Attention Is All You Need" in after
    for paper in ingest.PAPERS:
        assert f"arXiv:{paper}" not in cleaned(paper)[1]


def test_rule2_running_title_moshi_only():
    title = "Moshi: a speech-text foundation model for real-time dialogue"
    assert ingest.detect_running_title(raw(MOSHI)) == title
    assert ingest.detect_running_title(raw(ATT)) is None and ingest.detect_running_title(raw(DC)) is None
    assert ingest.first_line(raw(MOSHI)[5]) == title
    assert title not in cleaned(MOSHI)[5]
    assert cleaned(MOSHI)[1].count(title) == 1          # page 1: running title gone, real title kept


def test_rule3_page_number_footer():
    assert raw(ATT)[3].rstrip().splitlines()[-1].strip() == "3"
    after = ingest.strip_page_number(raw(ATT)[3], 3)
    assert after.rstrip().splitlines()[-1].strip() != "3" and "N = 6" in after
    assert ingest.strip_page_number("x\n 12\n", 13) == "x\n 12\n"  # only the page's own number


def test_rule3_page_number_on_layout_page():  # layout pages put the number far right / indented
    assert cleaned(ATT)[8].rstrip().splitlines()[-1].strip() != "8"


def test_rule4_permission_notice():
    after = ingest.strip_permission_notice(raw(ATT)[1])
    assert "Google hereby grants permission" not in after and "scholarly works" not in after
    assert "Ashish Vaswani" in after


def test_rule5_references_cut_attention():
    c = cleaned(ATT)
    assert "Table 4" in c[10]                            # content before the heading on p10 kept
    assert "References" not in c[10] and "[40] Muhua Zhu" not in c.get(12, "")
    assert c[11].strip() == ""                           # pure-reference page emptied, number kept as key
    assert "Attention Visualizations" in c[13] and "Figure 3:" in c[13]


def test_rule5_references_cut_moshi_keeps_appendix():
    c = cleaned(MOSHI)
    assert "Noatune Studios" in c[42]                    # acknowledgements before heading kept
    assert "References" not in c[42] and "Scott Aaronson" not in c[42]
    assert all(c[p].strip() == "" for p in range(43, 53))
    assert "Appendix A. Additional ablation on Mimi codec" in c[53]


def test_rule5_references_cut_dc_keeps_appendix():
    c = cleaned(DC)
    assert "Orevaoghene Ahia" not in c[25]
    assert "A Related Work" in c[31]                     # appendix heading kept (and joined, rule 8)


def test_rule5_missing_end_marker_fails_loudly():
    with pytest.raises(ValueError):
        ingest.cut_references({1: "References\n[1] x\n"}, ATT)


def test_rule6_figure_text_attention_word_lists():
    before = raw(ATT)[14]
    assert before.count("<pad>") > 0
    after = cleaned(ATT)[14]
    assert "<pad>" not in after and "Figure 4:" in after
    single = [l for l in after.splitlines() if len(l.split()) == 1]
    assert len(single) <= 2, single                      # word lists gone (was 79 single-word lines)


def test_rule6_figure_ticks_removed_on_plain_page_moshi():
    lines = {line.strip() for line in cleaned(MOSHI)[34].splitlines()}
    assert not lines & {"MosNet", "Q4_block=256", "Entropy"}  # plot legend/axis labels (non-table page)


def test_rule6_skips_table_pages_keeps_sparse_rows():
    rows = [" ".join(l.split()) for l in cleaned(ATT)[9].splitlines()]
    assert "0.2 4.95 25.5" in rows                       # Attention Table 3 row (dropout ablation)
    assert any(r.startswith("✓ 0.8 98.40") for r in (" ".join(l.split()) for l in cleaned(MOSHI)[37].splitlines()))
    after = cleaned(DC)[13]
    assert "H-Net (2-stage)" in after and "0.715" in after
    assert "1.05" in after                               # known cost: ticks on mixed plot+table page survive


def test_rule8_does_not_turn_decimals_into_headings():
    assert not any(" ".join(l.split()).startswith("1.00 Validation") for l in cleaned(DC)[13].splitlines())
    assert ingest.join_section_numbers("0.95\nValidation loss\n") == "0.95\nValidation loss\n"


def test_rule6_short_table_rows_survive_moshi():
    assert "135.4" in cleaned(MOSHI)[25]


def test_rule7_nfkc_math_italic():
    assert "𝑥" in raw(DC)[5]
    after = cleaned(DC)[5]
    assert not any("\U0001D400" <= ch <= "\U0001D7FF" for ch in after)  # no math-alphanumeric chars left
    assert "Chunk(x" in after.replace(" ", "")


def test_rule8_section_numbers_joined():
    assert "3.1 Encoder and Decoder Stacks" in cleaned(ATT)[3]
    assert "2.1.2 Design Principles" in cleaned(DC)[5]
    assert ingest.join_section_numbers("0\nThe initial input to the model is a long sentence that goes on well past eighty characters.\n") \
        .startswith("0\n")                               # long sentences are not headings


def test_layout_used_for_table_pages():
    _, layout_pages = ingest.load_raw(ATT)
    assert layout_pages == [6, 8, 9, 10]
    big = next(line for line in cleaned(ATT)[8].splitlines() if "Transformer (big)" in line)
    assert "28.4" in big and "41.8" in big               # row stays aligned


def test_original_page_numbers_kept():
    for paper, n in zip(ingest.PAPERS, (15, 66, 39)):
        assert sorted(cleaned(paper)) == list(range(1, n + 1))
