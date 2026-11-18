import os
import secrets

from flask import Flask

from . import db as dbm


def reset_database(path: str):
    """Drop the database file and rebuild schema + benchmark master data."""
    from .seed import seed_master
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = dbm.connect(path)
    dbm.init_schema(conn)
    with dbm.tx(conn):
        seed_master(conn)
    conn.close()


def create_app(test_config=None):
    app = Flask(__name__)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app.config.update(
        DATABASE=os.environ.get("OAMS_DB", os.path.join(root, "data", "oams.db")),
        SECRET_KEY=os.environ.get("OAMS_SECRET_KEY") or _persistent_secret(root),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 12,
    )
    if test_config:
        app.config.update(test_config)

    from . import web
    web.init_app(app)
    return app


def _persistent_secret(root):
    """Dev convenience: generate a secret once and keep it next to the database."""
    d = os.path.join(root, "data")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, ".secret_key")
    if not os.path.exists(p):
        with open(p, "w") as f:
            f.write(secrets.token_hex(32))
    with open(p) as f:
        return f.read().strip()
