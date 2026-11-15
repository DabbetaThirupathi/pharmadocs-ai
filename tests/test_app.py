import sqlite3
from pathlib import Path

import pytest

from app import create_app
from app import db as dbmod
from app.audit import verify_chain
from app.cli import seed
from app.rag import NOT_FOUND

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def client(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "t.db"), "UPLOAD_DIR": str(tmp_path / "up"),
                      "SECRET_KEY": "test-secret-key-that-is-long-enough-123", "TESTING": True})
    conn = dbmod.connect(app.config["DATABASE"])
    seed(conn, ROOT / "data" / "sops")
    app.extensions["retriever"].build(conn)
    conn.close()
    c = app.test_client()
    c.app = app
    return c


def login(c, u, p):
    r = c.post("/api/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200
    return {"Authorization": "Bearer " + r.json["token"]}


def test_login_rejects_bad_password(client):
    assert client.post("/api/auth/login", json={"username": "admin", "password": "x"}).status_code == 401


def test_ask_requires_auth(client):
    assert client.post("/api/ask", json={"question": "gowning"}).status_code == 401


def test_grounded_answer_with_citation(client):
    h = login(client, "op.anil", "Oper@1234")
    r = client.post("/api/ask", json={"question": "How often must gloves be sanitised in Grade A?"}, headers=h).json
    assert r["grounded"] and "15 minutes" in r["answer"]
    assert r["citations"][0]["doc_number"] == "SOP-PRD-001"
    assert r["citations"][0]["version"] == 2          # never the obsolete v1


def test_guard_refuses_out_of_scope(client):
    h = login(client, "op.anil", "Oper@1234")
    r = client.post("/api/ask", json={"question": "Who won the cricket world cup?"}, headers=h).json
    assert r["answer"] == NOT_FOUND and r["citations"] == []


def test_rbac_hides_qa_only_documents_from_operator(client):
    q = {"question": "Is retesting allowed before the Phase I OOS investigation is complete?"}
    op = client.post("/api/ask", json=q, headers=login(client, "op.anil", "Oper@1234")).json
    qa = client.post("/api/ask", json=q, headers=login(client, "qa.priya", "Qa@12345")).json
    assert all(c["doc_number"] != "SOP-QC-015" for c in op["citations"])
    assert qa["citations"][0]["doc_number"] == "SOP-QC-015"


def test_operator_cannot_upload_or_view_audit(client):
    h = login(client, "op.anil", "Oper@1234")
    assert client.get("/api/audit", headers=h).status_code == 403
    assert client.post("/api/documents", headers=h).status_code == 403


def test_new_effective_version_obsoletes_old(client, tmp_path):
    h = login(client, "qa.priya", "Qa@12345")
    f = tmp_path / "dp_v2.md"
    f.write_text("---\ndoc_number: SOP-ENG-009\ntitle: DP Monitoring\nversion: 2\ndepartment: ENGINEERING\n"
                 "status: EFFECTIVE\n---\n## 2. Limits\nA differential pressure of at least 12 Pa must be maintained.\n")
    with f.open("rb") as fh:
        r = client.post("/api/documents", data={"file": (fh, "dp_v2.md")}, headers=h,
                        content_type="multipart/form-data")
    assert r.status_code == 201
    ans = client.post("/api/ask", json={"question": "What is the minimum differential pressure?"}, headers=h).json
    assert "12 Pa" in ans["answer"] and "10 Pa" not in ans["answer"]
    docs = {(d["doc_number"], d["version"]): d["status"] for d in client.get("/api/documents", headers=h).json}
    assert docs[("SOP-ENG-009", 1)] == "OBSOLETE"


def test_status_change_requires_reason(client):
    h = login(client, "qa.priya", "Qa@12345")
    assert client.post("/api/documents/1/status", json={"status": "OBSOLETE"}, headers=h).status_code == 400


def test_deviation_draft_classifies_and_links_sops(client):
    h = login(client, "prod.ravi", "Prod@1234")
    desc = ("During filling of batch GLN2611A the differential pressure alarm between filling room "
            "and corridor stayed below 10 Pa for 12 minutes.")
    r = client.post("/api/deviations/draft", json={"description": desc}, headers=h)
    assert r.status_code == 201
    d = r.json
    assert d["classification"] == "MAJOR"
    assert d["batch_number"] == "GLN2611A"
    assert any(s["doc_number"] == "SOP-ENG-009" for s in d["related_sops"])
    assert d["status"].startswith("DRAFT")


def test_audit_chain_detects_tampering(client):
    h = login(client, "admin", "Admin@123")
    client.post("/api/ask", json={"question": "cold room temperature"}, headers=h)
    assert client.get("/api/audit/verify", headers=h).json["valid"] is True

    conn = sqlite3.connect(client.app.config["DATABASE"])
    with pytest.raises(sqlite3.DatabaseError):          # triggers block edits
        conn.execute("UPDATE audit_log SET username='hacker' WHERE id=1")
    # simulate a low-level tamper that bypasses triggers
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute("UPDATE audit_log SET username='hacker' WHERE id=2")
    conn.commit()
    conn.row_factory = sqlite3.Row
    res = verify_chain(conn)
    assert res["valid"] is False and res["broken_at"] == 2
