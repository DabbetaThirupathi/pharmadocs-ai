"""Document ingestion: text extraction, metadata parsing and section chunking.

Supported formats: .md / .txt (with optional YAML-like front matter), .pdf, .docx
"""
import hashlib
import re
from pathlib import Path

from .audit import log_event

HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
NUMBERED_HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+([A-Z][A-Za-z /&()\-]{2,80})$")
MAX_CHUNK_WORDS = 180
OVERLAP_WORDS = 30


class IngestError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# Text extraction
# --------------------------------------------------------------------------- #
def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".md", ".txt"):
        return path.read_text(encoding="utf-8")
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    if suffix == ".docx":
        import docx
        d = docx.Document(str(path))
        lines = []
        for p in d.paragraphs:
            style = (p.style.name or "").lower() if p.style is not None else ""
            if style.startswith("heading") and p.text.strip():
                lines.append("## " + p.text.strip())
            else:
                lines.append(p.text)
        return "\n".join(lines)
    raise IngestError(f"unsupported file type: {suffix}")


def parse_front_matter(text: str):
    """Split `---\\nkey: value\\n---` front matter from body."""
    meta = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].strip().splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip()
            text = text[end + 4:]
    return meta, text.strip()


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
def _split_long(section, words):
    if len(words) <= MAX_CHUNK_WORDS:
        return [(section, " ".join(words))]
    out, start = [], 0
    while start < len(words):
        end = min(start + MAX_CHUNK_WORDS, len(words))
        out.append((section, " ".join(words[start:end])))
        if end == len(words):
            break
        start = end - OVERLAP_WORDS
    return out


def chunk_by_section(body: str):
    """Return list of (section_title, text) chunks, split on headings."""
    sections, current_title, buf = [], "General", []

    def flush():
        words = " ".join(buf).split()
        if words:
            sections.extend(_split_long(current_title, words))

    for raw in body.splitlines():
        line = raw.strip()
        m = HEADING_RE.match(line) or NUMBERED_HEADING_RE.match(line)
        if m:
            flush()
            buf = []
            current_title = (m.group(2) if HEADING_RE.match(line) else f"{m.group(1)} {m.group(2)}").strip()
            continue
        if line:
            buf.append(line)
    flush()
    return sections


# --------------------------------------------------------------------------- #
# Persist
# --------------------------------------------------------------------------- #
def ingest_document(conn, path: Path, user=None, overrides=None):
    """Ingest one file. If a newer EFFECTIVE version arrives, older
    EFFECTIVE versions of the same doc_number become OBSOLETE."""
    raw = extract_text(path)
    meta, body = parse_front_matter(raw)
    meta.update({k: v for k, v in (overrides or {}).items() if v})

    required = ("doc_number", "title", "version", "department", "status")
    missing = [k for k in required if not meta.get(k)]
    if missing:
        raise IngestError(f"missing metadata: {', '.join(missing)}")

    status = meta["status"].upper()
    if status not in ("DRAFT", "EFFECTIVE", "OBSOLETE"):
        raise IngestError(f"invalid status {status}")
    try:
        version = int(meta["version"])
    except ValueError as exc:
        raise IngestError("version must be an integer") from exc

    checksum = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    exists = conn.execute(
        "SELECT id FROM documents WHERE doc_number=? AND version=?",
        (meta["doc_number"], version),
    ).fetchone()
    if exists:
        raise IngestError(f"{meta['doc_number']} v{version} already exists")

    if status == "EFFECTIVE":
        superseded = conn.execute(
            "SELECT id, version FROM documents WHERE doc_number=? AND status='EFFECTIVE'",
            (meta["doc_number"],),
        ).fetchall()
        for old in superseded:
            conn.execute("UPDATE documents SET status='OBSOLETE' WHERE id=?", (old["id"],))
            log_event(conn, "DOCUMENT_OBSOLETED", user, "document", old["id"],
                      {"doc_number": meta["doc_number"], "version": old["version"],
                       "superseded_by": version})

    cur = conn.execute(
        """INSERT INTO documents (doc_number, title, version, department, doc_type, status,
                                  access_level, effective_date, checksum, source_file, uploaded_by)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (meta["doc_number"], meta["title"], version, meta["department"].upper(),
         meta.get("doc_type", "SOP").upper(), status, meta.get("access_level", "ALL").upper(),
         meta.get("effective_date"), checksum, path.name, user["id"] if user else None),
    )
    doc_id = cur.lastrowid
    chunks = chunk_by_section(body)
    conn.executemany(
        "INSERT INTO chunks (document_id, section, ordinal, text) VALUES (?,?,?,?)",
        [(doc_id, sec, i, txt) for i, (sec, txt) in enumerate(chunks)],
    )
    conn.commit()
    log_event(conn, "DOCUMENT_INGESTED", user, "document", doc_id,
              {"doc_number": meta["doc_number"], "version": version, "status": status,
               "chunks": len(chunks), "sha256": checksum})
    return doc_id, len(chunks)


def ingest_folder(conn, folder: Path, user=None):
    results = []
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() in (".md", ".txt", ".pdf", ".docx"):
            try:
                results.append((p.name, *ingest_document(conn, p, user)))
            except IngestError as e:
                results.append((p.name, None, str(e)))
    return results
