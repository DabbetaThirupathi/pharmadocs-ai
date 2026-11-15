"""Flask CLI commands: `flask --app app seed` loads demo users + sample SOPs."""
from pathlib import Path

import click

from . import db as dbmod
from .auth import hash_password
from .ingest import ingest_folder

DEMO_USERS = [
    ("admin",    "System Administrator", "Admin@123", "ADMIN",      "IT"),
    ("qa.priya", "Priya Sharma",         "Qa@12345",  "QA",         "QA"),
    ("prod.ravi","Ravi Kumar",           "Prod@1234", "PRODUCTION", "PRODUCTION"),
    ("op.anil",  "Anil Reddy",           "Oper@1234", "OPERATOR",   "PRODUCTION"),
]


def seed(conn, sop_dir: Path):
    for u, name, pw, role, dept in DEMO_USERS:
        conn.execute(
            "INSERT OR IGNORE INTO users (username, full_name, password_hash, role, department) VALUES (?,?,?,?,?)",
            (u, name, hash_password(pw), role, dept))
    conn.commit()
    admin = conn.execute("SELECT * FROM users WHERE username='admin'").fetchone()
    return ingest_folder(conn, sop_dir, admin)


def register_cli(app):
    @app.cli.command("seed")
    @click.option("--sops", default=str(Path(__file__).resolve().parent.parent / "data" / "sops"))
    def seed_cmd(sops):
        """Create demo users and ingest sample SOPs."""
        conn = dbmod.connect(app.config["DATABASE"])
        dbmod.init_schema(conn)
        for name, doc_id, info in seed(conn, Path(sops)):
            click.echo(f"{'OK ' if doc_id else 'SKIP'} {name}: {info}")
        app.extensions["retriever"].build(conn)
        conn.close()
        click.echo("Demo logins: admin/Admin@123, qa.priya/Qa@12345, prod.ravi/Prod@1234, op.anil/Oper@1234")
