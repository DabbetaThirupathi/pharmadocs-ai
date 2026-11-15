"""PharmaDocs AI — GMP knowledge assistant (Flask application factory)."""
import os
from pathlib import Path

from flask import Flask

from . import db as dbmod
from .retriever import HybridRetriever
from .rag import RAGPipeline

BASE_DIR = Path(__file__).resolve().parent.parent


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("SECRET_KEY", "dev-change-me"),
        DATABASE=os.getenv("DATABASE", str(BASE_DIR / "instance" / "pharmadocs.db")),
        UPLOAD_DIR=os.getenv("UPLOAD_DIR", str(BASE_DIR / "instance" / "uploads")),
        TOKEN_TTL_MIN=int(os.getenv("TOKEN_TTL_MIN", "120")),
        MIN_CONFIDENCE=float(os.getenv("MIN_CONFIDENCE", "0.08")),
        USE_DENSE=os.getenv("USE_DENSE", "0") == "1",
        MAX_CONTENT_LENGTH=20 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)

    conn = dbmod.connect(app.config["DATABASE"])
    dbmod.init_schema(conn)
    retriever = HybridRetriever(use_dense=app.config["USE_DENSE"]).build(conn)
    conn.close()

    app.extensions["retriever"] = retriever
    app.extensions["rag"] = RAGPipeline(retriever, min_confidence=app.config["MIN_CONFIDENCE"])
    app.teardown_appcontext(dbmod.close_db)

    from .routes import bp
    app.register_blueprint(bp)

    from .cli import register_cli
    register_cli(app)
    return app
