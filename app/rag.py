"""Retrieval-Augmented Generation pipeline with a hallucination guard."""
import re

from .llm import LLMClient
from .retriever import Hit, tokenize, expand_query

NOT_FOUND = "Not found in approved documents. Please contact QA."
SENT_SPLIT = re.compile(r"(?<=[.;])\s+(?=[A-Z0-9])")


def cite(h: Hit) -> str:
    return f"[{h.doc_number} v{h.version} §{h.section}]"


def build_context(hits):
    return "\n\n".join(f"{cite(h)}\n{h.text}" for h in hits)


def extractive_answer(question: str, hits, max_sentences: int = 3) -> str:
    """Offline fallback: pick the sentences with the highest query-term coverage.

    A sentence is kept only if it covers enough of the (non-stopword) query
    terms; otherwise the guard answer is returned instead of a weak guess.
    """
    q_core = set(tokenize(question))
    q_all = set(expand_query(list(q_core)))
    if not q_core:
        return NOT_FOUND
    scored = []
    for rank, h in enumerate(hits[:3]):
        for pos, sent in enumerate(SENT_SPLIT.split(h.text)):
            toks = set(tokenize(sent + " " + h.section))
            core_hit = len(q_core & toks)
            if core_hit == 0:
                continue
            coverage = core_hit / len(q_core)
            score = coverage + 0.1 * len(q_all & toks) / len(q_all) - 0.08 * rank - 0.005 * pos
            scored.append((score, coverage, rank, pos, sent.strip(), h))
    if not scored:
        return NOT_FOUND
    scored.sort(key=lambda x: -x[0])
    best_cov = scored[0][1]
    if best_cov < 0.34:
        return NOT_FOUND
    keep = [x for x in scored if x[1] >= max(0.34, best_cov * 0.6)][:max_sentences]
    keep.sort(key=lambda x: (x[2], x[3]))  # restore document order
    return "\n".join(f"- {s} {cite(h)}" for _, _, _, _, s, h in keep)


class RAGPipeline:
    def __init__(self, retriever, min_confidence: float = 0.08, top_k: int = 4):
        self.retriever = retriever
        self.min_confidence = min_confidence
        self.top_k = top_k
        self.llm = LLMClient()

    def answer(self, question: str, access_levels: set, department=None):
        hits = self.retriever.search(question, access_levels, k=self.top_k, department=department)
        confidence = round(max((h.lexical for h in hits), default=0.0), 3)

        if not hits or confidence < self.min_confidence:
            return {"answer": NOT_FOUND, "citations": [], "confidence": confidence,
                    "grounded": False, "mode": "guard"}

        mode = "extractive"
        text = None
        if self.llm.enabled:
            try:
                text = self.llm.chat(question, build_context(hits))
                mode = "llm"
            except Exception:  # network/LLM failure -> safe fallback
                text = None
        if text is None:
            text = extractive_answer(question, hits)

        grounded = text != NOT_FOUND
        return {
            "answer": text,
            "confidence": confidence,
            "grounded": grounded,
            "mode": mode,
            "citations": [{
                "doc_number": h.doc_number, "title": h.title, "version": h.version,
                "section": h.section, "score": round(h.score, 4), "snippet": h.text[:240],
            } for h in hits] if grounded else [],
        }
