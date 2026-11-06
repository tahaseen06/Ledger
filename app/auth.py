import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import g, request

from .ledger import ApiError, now, _security_event

SESSION_HOURS = 12
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


def hash_password(pw):
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${dk.hex()}"


def check_password(pw, stored):
    try:
        _, salt, dk = stored.split("$")
        cand = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
        return hmac.compare_digest(cand.hex(), dk)
    except Exception:
        return False


def _th(token):
    return hashlib.sha256(token.encode()).hexdigest()


def login(conn, username, password):
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=LOCKOUT_MINUTES)).isoformat(timespec="microseconds")
    fails = conn.execute("SELECT COUNT(*) c FROM security_events WHERE action='login_failed' AND username=? AND created_at>=?",
                         (username, cutoff)).fetchone()["c"]
    if fails >= MAX_FAILED_LOGINS:
        raise ApiError(429, "locked", "too many failed logins; try again later")
    u = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    ok = check_password(password, u["pw_hash"]) if u else check_password(password, "scrypt$00$00")  # constant-ish work
    if not (u and ok):
        _security_event(conn, {"id": u["id"] if u else None, "username": username[:64]}, "login_failed", None)
        raise ApiError(401, "bad_credentials", "invalid username or password")
    token = secrets.token_urlsafe(32)
    exp = (datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)).isoformat(timespec="microseconds")
    conn.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)", (_th(token), u["id"], exp))
    return token, u


def current_user(conn):
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        raise ApiError(401, "unauthenticated", "missing bearer token")
    row = conn.execute("SELECT u.*, s.expires_at FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
                       (_th(h[7:].strip()),)).fetchone()
    if not row or row["expires_at"] < now():
        raise ApiError(401, "unauthenticated", "invalid or expired token")
    return row


def logout(conn):
    h = request.headers.get("Authorization", "")
    conn.execute("DELETE FROM sessions WHERE token_hash=?", (_th(h[7:].strip()),))


def require(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            g.user = current_user(g.conn)
            if g.user["role"] not in roles:
                _security_event(g.conn, g.user, "denied", f"role {g.user['role']} called {request.method} {request.path}")
                raise ApiError(403, "forbidden", "not permitted")
            return fn(*a, **kw)
        return wrapper
    return deco
