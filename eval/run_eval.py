"""Offline evaluation of PharmaDocs AI retrieval + answering.

Metrics
-------
hit@1 / hit@3     : expected SOP appears in the top-1 / top-3 retrieved chunks
answer_accuracy   : expected key fact appears in the generated answer
guard_precision   : unanswerable / unauthorised questions correctly refused
citation_validity : every citation in an answer points to an EFFECTIVE document

Run:  python eval/run_eval.py            (prints a table + writes eval/results.json)
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import create_app                      # noqa: E402
from app import db as dbmod                      # noqa: E402
from app.auth import allowed_access_levels       # noqa: E402
from app.cli import seed                         # noqa: E402
from app.rag import NOT_FOUND                    # noqa: E402


def main():
    tmp = tempfile.mkdtemp()
    app = create_app({"DATABASE": f"{tmp}/eval.db", "UPLOAD_DIR": f"{tmp}/up"})
    conn = dbmod.connect(app.config["DATABASE"])
    seed(conn, ROOT / "data" / "sops")
    retriever = app.extensions["retriever"].build(conn)
    rag = app.extensions["rag"]
    effective = {r["doc_number"] + f" v{r['version']}" for r in
                 conn.execute("SELECT doc_number, version FROM documents WHERE status='EFFECTIVE'")}

    cases = json.loads((ROOT / "eval" / "questions.json").read_text())
    rows = []
    h1 = h3 = acc = guard_ok = n_ans = n_guard = cites_ok = cites_total = 0
    for c in cases:
        levels = allowed_access_levels(c.get("role", "QA"))
        hits = retriever.search(c["q"], levels, k=3)
        res = rag.answer(c["q"], levels)
        docs = [h.doc_number for h in hits]
        for ci in res["citations"]:
            cites_total += 1
            cites_ok += f"{ci['doc_number']} v{ci['version']}" in effective
        if c["doc"] is None:
            n_guard += 1
            ok = res["answer"] == NOT_FOUND
            guard_ok += ok
            rows.append((c["q"][:60], "—", "refused" if ok else "ANSWERED", "PASS" if ok else "FAIL"))
            continue
        n_ans += 1
        r1 = bool(docs) and docs[0] == c["doc"]
        r3 = c["doc"] in docs
        a = c["expect"].lower() in res["answer"].lower()
        h1 += r1; h3 += r3; acc += a
        rows.append((c["q"][:60], "top1" if r1 else ("top3" if r3 else "MISS"),
                     "correct" if a else "WRONG", "PASS" if (r3 and a) else "FAIL"))

    metrics = {
        "questions": len(cases),
        "hit@1": round(h1 / n_ans, 3),
        "hit@3": round(h3 / n_ans, 3),
        "answer_accuracy": round(acc / n_ans, 3),
        "guard_precision": round(guard_ok / n_guard, 3) if n_guard else None,
        "citation_validity": round(cites_ok / cites_total, 3) if cites_total else None,
        "answer_mode": "llm" if rag.llm.enabled else "extractive (offline)",
    }
    w = max(len(r[0]) for r in rows)
    print(f"{'Question'.ljust(w)}  Retrieval  Answer    Result")
    for r in rows:
        print(f"{r[0].ljust(w)}  {r[1]:<9}  {r[2]:<8}  {r[3]}")
    print("\n" + json.dumps(metrics, indent=2))
    (ROOT / "eval" / "results.json").write_text(json.dumps({"metrics": metrics, "rows": rows}, indent=2))
    conn.close()
    return metrics


if __name__ == "__main__":
    main()
