"""Phase 5 corrupted-answer test: three deterministic corruptions of a judge-SUPPORTED answer (no model calls).
Each returns the corrupted text, or None when that corruption does not apply."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_questions import contains  # noqa: E402

LABEL = re.compile(r"\[\s*S(\d+)\s*\]")
# Fabricated claim for "unsupported addition"; checked absent from the library in tests.
FABRICATED = "The model was also trained on 512 TPU v4 chips for 3 weeks"


def _outside_labels(text):
    """Spans of text that are not citation labels."""
    spans, last = [], 0
    for m in LABEL.finditer(text):
        spans.append((last, m.start()))
        last = m.end()
    return spans + [(last, len(text))]


def number_swap(text):
    """Change the FIRST number outside citation labels: its leading digit +1 (9 → 1). 4000→5000, 0.1→1.1, 12.5→22.5."""
    for a, b in _outside_labels(text):
        m = re.search(r"\d+(?:[.,]\d+)*", text[a:b])
        if m:
            i = a + m.start()
            return text[:i] + str((int(text[i]) + 1) % 10 or 1) + text[i + 1:]
    return None


def citation_swap(text, retrieved_texts, evidence_phrase):
    """Re-point every cited label to ONE retrieved chunk that does not contain the evidence phrase and is not cited."""
    cited = {int(n) for n in LABEL.findall(text)}
    for n, chunk_text in enumerate(retrieved_texts, 1):
        if n not in cited and not contains(chunk_text, evidence_phrase):
            return LABEL.sub(f"[S{n}]", text)
    return None


def unsupported_addition(text):
    """Append a fabricated claim cited to the answer's first label (so the deterministic check still passes)."""
    m = LABEL.search(text)
    if not m:
        return None
    return f"{text.rstrip()} {FABRICATED} [S{m.group(1)}]."


CORRUPTIONS = ("number_swap", "citation_swap", "unsupported_addition")
