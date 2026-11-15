"""REST API + web UI routes."""
import json
from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, render_template, request
from werkzeug.utils import secure_filename

from .audit import log_event, verify_chain
from .auth import (ROLES, allowed_access_levels, hash_password, issue_token,
                   login_required, verify_password)
from .db import get_db
from .deviation import draft_report
from .ingest import IngestError, ingest_document

bp = Blueprint("api", __name__)
ALLOWED_EXT = {".md", ".txt", ".pdf", ".docx"}


def _rebuild_index():
    current_app.extensions["retriever"].build(get_db())


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
@bp.get("/")
def index():
    return render_template("index.html")


@bp.get("/api/health")
def health():
    rag = current_app.extensions["rag"]
    return jsonify(status="ok", indexed_chunks=len(current_app.extensions["retriever"].rows),
                   llm_enabled=rag.llm.enabled)


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
@bp.post("/api/auth/login")
def login():
    data = request.get_json(silent=True) or {}
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE username=? AND is_active=1",
                      (data.get("username", ""),)).fetchone()
    if not user or not verify_password(user["password_hash"], data.get("password", "")):
        log_event(db, "LOGIN_FAILED", None, "user", None, {"username": data.get("username")})
        return jsonify(error="invalid credentials"), 401
    log_event(db, "LOGIN_SUCCESS", user, "user", user["id"])
    return jsonify(token=issue_token(user),
                   user={"username": user["username"], "full_name": user["full_name"],
                         "role": user["role"], "department": user["department"]})


@bp.post("/api/users")
@login_required("ADMIN")
def create_user():
    data = request.get_json(silent=True) or {}
    role = (data.get("role") or "").upper()
    if role not in ROLES or not data.get("username") or len(data.get("password", "")) < 8:
        return jsonify(error="username, password (>=8 chars) and valid role required"), 400
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO users (username, full_name, password_hash, role, department) VALUES (?,?,?,?,?)",
            (data["username"], data.get("full_name", data["username"]), hash_password(data["password"]),
             role, (data.get("department") or "GENERAL").upper()))
        db.commit()
    except Exception:
        return jsonify(error="username already exists"), 409
    log_event(db, "USER_CREATED", g.user, "user", cur.lastrowid, {"username": data["username"], "role": role})
    return jsonify(id=cur.lastrowid), 201


# --------------------------------------------------------------------------- #
# Ask
# --------------------------------------------------------------------------- #
@bp.post("/api/ask")
@login_required()
def ask():
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    if not question or len(question) > 1000:
        return jsonify(error="question required (max 1000 chars)"), 400
    result = current_app.extensions["rag"].answer(
        question, allowed_access_levels(g.user["role"]), department=data.get("department"))
    log_event(get_db(), "QUERY", g.user, "query", None, {
        "question": question, "grounded": result["grounded"], "confidence": result["confidence"],
        "mode": result["mode"],
        "sources": [f"{c['doc_number']} v{c['version']}" for c in result["citations"]],
    })
    return jsonify(result)


# --------------------------------------------------------------------------- #
# Documents
# --------------------------------------------------------------------------- #
@bp.get("/api/documents")
@login_required()
def list_documents():
    levels = allowed_access_levels(g.user["role"])
    q = "SELECT id, doc_number, title, version, department, doc_type, status, access_level, effective_date, uploaded_at FROM documents"
    rows = [dict(r) for r in get_db().execute(q + " ORDER BY doc_number, version DESC").fetchall()]
    return jsonify([r for r in rows if r["access_level"] in levels])


@bp.post("/api/documents")
@login_required("QA", "ADMIN")
def upload_document():
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify(error="file required"), 400
    name = secure_filename(f.filename)
    if Path(name).suffix.lower() not in ALLOWED_EXT:
        return jsonify(error=f"allowed types: {sorted(ALLOWED_EXT)}"), 400
    dest = Path(current_app.config["UPLOAD_DIR"]) / name
    f.save(dest)
    overrides = {k: request.form.get(k) for k in
                 ("doc_number", "title", "version", "department", "status", "access_level", "effective_date")}
    try:
        doc_id, n = ingest_document(get_db(), dest, g.user, overrides)
    except IngestError as e:
        return jsonify(error=str(e)), 400
    _rebuild_index()
    return jsonify(id=doc_id, chunks=n), 201


@bp.post("/api/documents/<int:doc_id>/status")
@login_required("QA", "ADMIN")
def change_status(doc_id):
    data = request.get_json(silent=True) or {}
    new = (data.get("status") or "").upper()
    reason = (data.get("reason") or "").strip()
    if new not in ("DRAFT", "EFFECTIVE", "OBSOLETE") or not reason:
        return jsonify(error="valid status and reason required (Part 11: reason for change)"), 400
    db = get_db()
    doc = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc:
        return jsonify(error="not found"), 404
    if new == "EFFECTIVE":
        db.execute("UPDATE documents SET status='OBSOLETE' WHERE doc_number=? AND status='EFFECTIVE' AND id<>?",
                   (doc["doc_number"], doc_id))
    db.execute("UPDATE documents SET status=? WHERE id=?", (new, doc_id))
    db.commit()
    log_event(db, "DOCUMENT_STATUS_CHANGED", g.user, "document", doc_id,
              {"doc_number": doc["doc_number"], "version": doc["version"],
               "from": doc["status"], "to": new, "reason": reason})
    _rebuild_index()
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
# Deviations
# --------------------------------------------------------------------------- #
@bp.post("/api/deviations/draft")
@login_required()
def deviation_draft():
    data = request.get_json(silent=True) or {}
    desc = (data.get("description") or "").strip()
    if len(desc) < 20:
        return jsonify(error="describe the event in at least 20 characters"), 400
    db = get_db()
    report = draft_report(db, desc, current_app.extensions["retriever"],
                          allowed_access_levels(g.user["role"]), user_id=g.user["id"])
    log_event(db, "DEVIATION_DRAFTED", g.user, "deviation", report["dev_number"],
              {"classification": report["classification"], "area": report["area"]})
    return jsonify(report), 201


@bp.get("/api/deviations")
@login_required("QA", "ADMIN", "PRODUCTION")
def deviation_list():
    rows = get_db().execute(
        "SELECT dev_number, created_at, area, classification, status FROM deviations ORDER BY id DESC").fetchall()
    return jsonify([dict(r) for r in rows])


# --------------------------------------------------------------------------- #
# Audit trail
# --------------------------------------------------------------------------- #
@bp.get("/api/audit")
@login_required("QA", "ADMIN")
def audit_list():
    limit = min(int(request.args.get("limit", 100)), 1000)
    rows = get_db().execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["details"] = json.loads(d["details"]) if d["details"] else None
        out.append(d)
    return jsonify(out)


@bp.get("/api/audit/verify")
@login_required("QA", "ADMIN")
def audit_verify():
    return jsonify(verify_chain(get_db()))
