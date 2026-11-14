"""Generator, judge and correction prompts (Phase 4). Shown to the user before any real call."""

PAPER_NAMES = {"attention": "Attention Is All You Need", "moshi": "Moshi", "dynchunk": "Dynamic Chunking (H-Net)"}
NOT_IN_PAPERS = "NOT_IN_PAPERS"

GENERATOR_SYSTEM = """You answer questions about three research papers using ONLY the numbered sources provided.
Rules:
1. Use only facts stated in the sources. Do not use outside knowledge, even if you know the answer.
2. After every sentence that states a fact, cite the source label(s) it comes from in square brackets, e.g. [S2] or [S1][S4].
3. If the sources do not contain the answer, reply with exactly NOT_IN_PAPERS and nothing else.
4. Answer in at most 5 sentences. Copy numbers exactly as written in the sources.
5. Cite only labels that appear in the sources."""

JUDGE_SYSTEM = """You check whether an answer is supported by the source passages it cites.
For every factual claim in the answer, decide whether the cited passages state it. Numbers must match exactly.
A claim that is not stated in the cited passages is unsupported, even if it is true.
Reply with JSON only, no other text:
{"verdict": "supported" or "unsupported", "claim": "<the first unsupported claim, or empty>", "reason": "<one short sentence>"}"""

CORRECTION = """A checker found a problem with your answer.
Unsupported claim: {claim}
Reason: {reason}
Rewrite the answer using only the sources, removing or correcting unsupported claims, and cite every factual sentence.
If the sources do not answer the question, reply with exactly NOT_IN_PAPERS and nothing else."""


def pages(c):
    return f"p.{c['page_start']}" if c["page_start"] == c["page_end"] else f"pp.{c['page_start']}–{c['page_end']}"


def source_block(labelled):
    """labelled: [(label_number, chunk)] → text block with paper, pages, section per source."""
    return "\n\n".join(f"[S{n}] ({PAPER_NAMES[c['paper']]}, {pages(c)}, §{c['section_heading']})\n{c['text']}"
                       for n, c in labelled)


def generator_messages(question, chunks):
    return [{"role": "system", "content": GENERATOR_SYSTEM},
            {"role": "user", "content": f"Sources:\n\n{source_block(enumerate(chunks, 1))}\n\nQuestion: {question}"}]


def judge_messages(question, answer, cited):
    return [{"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": f"Question: {question}\n\nAnswer:\n{answer}\n\nCited sources:\n\n{source_block(cited)}"}]


def correction_messages(gen_messages, answer, claim, reason):
    return gen_messages + [{"role": "assistant", "content": answer},
                           {"role": "user", "content": CORRECTION.format(claim=claim or "(not given)", reason=reason or "(not given)")}]
