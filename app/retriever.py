"""Hybrid retriever: BM25 (keyword) + TF-IDF cosine (lexical-semantic),
optionally + dense embeddings (sentence-transformers) when installed.

Scores are fused with Reciprocal Rank Fusion (RRF). Only chunks from
EFFECTIVE documents that the caller's role may read are ever returned.
"""
import math
import re
from collections import Counter
from dataclasses import dataclass

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.\-][a-z0-9]+)*")
STOP = set("""a an the of to in on for and or is are be by with as at from that this it
its into than then there these those which who whom what when where how do does can
should must shall will may i we you they our your their was were been being if not
procedure sop document documents per used use please tell me about long often many much""".split())

# Domain synonyms improve recall for pharma vocabulary.
SYNONYMS = {   # keys and values are stems
    "gown": ["garment", "coverall"],
    "autoclave": ["steril", "steam"],
    "steril": ["autoclave"],
    "capa": ["corrective", "preventive"],
    "devi": ["incident", "nonconformance"],
    "em": ["environmental", "monitoring"],
    "cleanroom": ["grade", "aseptic"],
    "hold": ["expiry"],
    "oos": ["out", "specification"],
}


IRREGULAR = {"held": "hold", "kept": "keep", "worn": "wear", "done": "do"}
SUFFIXES = ("isations", "isation", "ising", "ised", "isers", "iser", "ises", "ise",
            "ations", "ation", "ings", "ing", "ers", "er", "ed", "es", "s")


def stem(t: str) -> str:
    """Tiny suffix-stripping stemmer (UK/US spelling aware)."""
    if t in IRREGULAR:
        return IRREGULAR[t]
    t = t.replace("iz", "is")
    for suf in SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[: -len(suf)]
    return t


def tokenize(text: str):
    return [stem(t) for t in TOKEN_RE.findall(text.lower()) if t not in STOP]


def expand_query(tokens):
    """Expects already-stemmed tokens."""
    out = list(tokens)
    for t in tokens:
        out.extend(SYNONYMS.get(t, []))
    return out


@dataclass
class Hit:
    chunk_id: int
    document_id: int
    doc_number: str
    title: str
    version: int
    section: str
    text: str
    score: float          # fused RRF score
    lexical: float        # TF-IDF cosine of the best match (0..1), used for confidence


class BM25:
    def __init__(self, docs_tokens, k1=1.5, b=0.75):
        self.k1, self.b = k1, b
        self.docs = docs_tokens
        self.N = len(docs_tokens)
        self.avgdl = (sum(len(d) for d in docs_tokens) / self.N) if self.N else 0
        df = Counter()
        for d in docs_tokens:
            df.update(set(d))
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}
        self.tf = [Counter(d) for d in docs_tokens]

    def scores(self, q_tokens):
        out = np.zeros(self.N)
        for i, tf in enumerate(self.tf):
            dl = len(self.docs[i])
            s = 0.0
            for t in q_tokens:
                if t not in tf:
                    continue
                f = tf[t]
                s += self.idf.get(t, 0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1)))
            out[i] = s
        return out


class HybridRetriever:
    RRF_K = 60

    def __init__(self, use_dense: bool = False):
        self.rows = []
        self.use_dense = use_dense
        self._dense_model = None
        self._dense = None

    # ------------------------------------------------------------------ #
    def build(self, conn):
        self.rows = [dict(r) for r in conn.execute(
            """SELECT c.id AS chunk_id, c.document_id, c.section, c.text,
                      d.doc_number, d.title, d.version, d.status, d.access_level, d.department
               FROM chunks c JOIN documents d ON d.id = c.document_id
               WHERE d.status = 'EFFECTIVE'
               ORDER BY c.id""").fetchall()]
        corpus = [f"{r['title']} {r['section']} {r['text']}" for r in self.rows]
        self.tokens = [tokenize(t) for t in corpus]
        if not self.rows:
            self.bm25, self.tfidf, self.matrix = None, None, None
            return self
        self.bm25 = BM25(self.tokens)
        self.tfidf = TfidfVectorizer(tokenizer=tokenize, lowercase=False, ngram_range=(1, 2),
                                     sublinear_tf=True, token_pattern=None)
        self.matrix = self.tfidf.fit_transform(corpus)
        if self.use_dense:
            self._build_dense(corpus)
        return self

    def _build_dense(self, corpus):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            self.use_dense = False
            return
        self._dense_model = SentenceTransformer("all-MiniLM-L6-v2")
        self._dense = self._dense_model.encode(corpus, normalize_embeddings=True)

    # ------------------------------------------------------------------ #
    def search(self, query: str, access_levels: set, k: int = 5, department: str | None = None):
        if not self.rows:
            return []
        allowed = np.array([
            (r["access_level"] in access_levels) and (department is None or r["department"] == department)
            for r in self.rows
        ])
        if not allowed.any():
            return []

        q_tokens = expand_query(tokenize(query))
        bm = self.bm25.scores(q_tokens)
        tf = (self.matrix @ self.tfidf.transform([" ".join(q_tokens)]).T).toarray().ravel()

        rankings = [bm, tf]
        if self.use_dense and self._dense is not None:
            qv = self._dense_model.encode([query], normalize_embeddings=True)[0]
            rankings.append(self._dense @ qv)

        fused = np.zeros(len(self.rows))
        for scores in rankings:
            s = np.where(allowed, scores, -np.inf)
            order = np.argsort(-s)
            for rank, idx in enumerate(order):
                if not allowed[idx] or scores[idx] <= 0:
                    continue
                fused[idx] += 1.0 / (self.RRF_K + rank + 1)

        order = [i for i in np.argsort(-fused) if fused[i] > 0][:k]
        return [Hit(
            chunk_id=self.rows[i]["chunk_id"], document_id=self.rows[i]["document_id"],
            doc_number=self.rows[i]["doc_number"], title=self.rows[i]["title"],
            version=self.rows[i]["version"], section=self.rows[i]["section"],
            text=self.rows[i]["text"], score=float(fused[i]), lexical=float(tf[i]),
        ) for i in order]
