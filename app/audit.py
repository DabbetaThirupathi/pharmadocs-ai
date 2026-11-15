"""Tamper-evident audit trail.

Every entry stores the SHA-256 hash of the previous entry, forming a hash
chain. Editing or deleting any historical row breaks the chain, and
`verify_chain` reports the first broken entry. SQLite triggers additionally
block UPDATE/DELETE on the table.

Inspired by the audit-trail expectations of US FDA 21 CFR Part 11 and
EU GMP Annex 11 (who did what, when, and why — attributable, contemporaneous).
"""
import hashlib
import json
from datetime import datetime, timezone

GENESIS = "0" * 64


def _hash(prev_hash, ts, user_id, username, action, entity, entity_id, details):
    payload = json.dumps(
        [prev_hash, ts, user_id, username, action, entity, entity_id, details],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def log_event(conn, action, user=None, entity=None, entity_id=None, details=None):
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    user_id = user["id"] if user else None
    username = user["username"] if user else "system"
    details_s = json.dumps(details, ensure_ascii=False, sort_keys=True) if details is not None else None
    entity_id_s = str(entity_id) if entity_id is not None else None

    row = conn.execute("SELECT entry_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = row["entry_hash"] if row else GENESIS
    entry_hash = _hash(prev_hash, ts, user_id, username, action, entity, entity_id_s, details_s)

    conn.execute(
        """INSERT INTO audit_log (ts, user_id, username, action, entity, entity_id, details, prev_hash, entry_hash)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (ts, user_id, username, action, entity, entity_id_s, details_s, prev_hash, entry_hash),
    )
    conn.commit()
    return entry_hash


def verify_chain(conn):
    """Return {'valid': bool, 'entries': n, 'broken_at': id|None}."""
    prev = GENESIS
    rows = conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
    for r in rows:
        expected = _hash(prev, r["ts"], r["user_id"], r["username"], r["action"],
                         r["entity"], r["entity_id"], r["details"])
        if r["prev_hash"] != prev or r["entry_hash"] != expected:
            return {"valid": False, "entries": len(rows), "broken_at": r["id"]}
        prev = r["entry_hash"]
    return {"valid": True, "entries": len(rows), "broken_at": None}
