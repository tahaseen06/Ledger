import logging
import sqlite3

from flask import Flask, g, jsonify, send_from_directory

from . import db
from .ledger import ApiError
from .routes import bp

log = logging.getLogger("ledgerline")


def create_app(db_path=None):
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config["DB_PATH"] = db_path or db.db_path()
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
    db.init_db(app.config["DB_PATH"])

    @app.before_request
    def open_conn():
        g.conn = db.connect(app.config["DB_PATH"])

    @app.teardown_request
    def close_conn(exc):
        conn = g.pop("conn", None)
        if conn is not None:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"
        if resp.mimetype == "application/json":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(ApiError)
    def api_error(e):
        return jsonify({"error": e.code, "message": e.message}), e.status

    @app.errorhandler(sqlite3.OperationalError)
    def busy(e):
        # Could not get the write lock in time. Nothing was committed; the client may retry with the same key.
        log.warning("database busy/locked: %s", e)
        r = jsonify({"error": "busy", "message": "temporarily unavailable; retry with the same Idempotency-Key"})
        r.status_code = 503
        r.headers["Retry-After"] = "1"
        return r

    @app.errorhandler(404)
    def nf(e):
        return jsonify({"error": "not_found", "message": "not found"}), 404

    @app.errorhandler(405)
    def na(e):
        return jsonify({"error": "method_not_allowed", "message": "method not allowed"}), 405

    @app.errorhandler(413)
    def big(e):
        return jsonify({"error": "too_large", "message": "request too large"}), 413

    @app.errorhandler(Exception)
    def boom(e):
        log.exception("unhandled error")
        return jsonify({"error": "internal", "message": "internal error; nothing was partially applied"}), 500

    app.register_blueprint(bp)

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    return app
