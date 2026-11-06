import secrets
import sqlite3

from flask import Blueprint, g, jsonify, request

from . import auth, ledger
from .ledger import ApiError, now, _security_event

bp = Blueprint("api", __name__, url_prefix="/api")


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "JSON object body required")
    return data


def can_see(user, acct):
    return user["role"] in ("admin", "auditor") or acct["owner_id"] == user["id"]


def acct_json(a, owner=None):
    return {"number": a["number"], "name": a["name"], "balance_paise": a["balance"],
            "overdraft_limit_paise": a["overdraft_limit"], "frozen": bool(a["frozen"]),
            "owner": owner, "currency": "INR"}


def get_account(number):
    a = g.conn.execute("SELECT a.*, u.username AS owner FROM accounts a LEFT JOIN users u ON u.id=a.owner_id "
                       "WHERE a.number=? AND a.kind='customer'", (number,)).fetchone()
    # Unknown account and "not yours" look identical to a holder.
    if not a or not can_see(g.user, a):
        raise ApiError(404, "account_not_found", "account not found")
    return a


# ------------------------------------------------------------------ auth
@bp.post("/auth/login")
def login():
    d = body()
    if not isinstance(d.get("username"), str) or not isinstance(d.get("password"), str):
        raise ApiError(400, "invalid_json", "username and password required")
    token, u = auth.login(g.conn, d["username"], d["password"])
    return jsonify({"token": token, "role": u["role"], "username": u["username"]})


@bp.post("/auth/logout")
def logout():
    auth.current_user(g.conn)
    auth.logout(g.conn)
    return jsonify({"ok": True})


@bp.get("/me")
@auth.require("admin", "holder", "auditor")
def me():
    return jsonify({"username": g.user["username"], "role": g.user["role"]})


# ------------------------------------------------------------------ users & accounts (admin)
@bp.post("/users")
@auth.require("admin")
def create_user():
    d = body()
    u, p, r = d.get("username"), d.get("password"), d.get("role")
    if not (isinstance(u, str) and 3 <= len(u) <= 32 and u.replace("_", "").replace(".", "").isalnum()):
        raise ApiError(400, "invalid_username", "username: 3-32 chars, letters/digits/_/.")
    if not (isinstance(p, str) and 8 <= len(p) <= 128):
        raise ApiError(400, "weak_password", "password must be 8-128 chars")
    if r not in ("admin", "holder", "auditor"):
        raise ApiError(400, "invalid_role", "role must be admin, holder or auditor")
    try:
        g.conn.execute("INSERT INTO users(username,pw_hash,role,created_at) VALUES(?,?,?,?)",
                       (u, auth.hash_password(p), r, now()))
    except sqlite3.IntegrityError:
        raise ApiError(409, "username_taken", "username already exists")
    _security_event(g.conn, g.user, "admin_create_user", f"{u} as {r}")
    return jsonify({"username": u, "role": r}), 201


@bp.get("/users")
@auth.require("admin", "auditor")
def list_users():
    rows = g.conn.execute("SELECT username, role, created_at FROM users ORDER BY id").fetchall()
    return jsonify([dict(r) for r in rows])


@bp.post("/accounts")
@auth.require("admin")
def create_account():
    d = body()
    owner = g.conn.execute("SELECT * FROM users WHERE username=? AND role='holder'", (d.get("owner_username"),)).fetchone()
    if not owner:
        raise ApiError(404, "owner_not_found", "owner_username must be an existing account holder")
    name = d.get("name")
    if not (isinstance(name, str) and 1 <= len(name) <= 80):
        raise ApiError(400, "invalid_name", "name: 1-80 chars")
    od = d.get("overdraft_limit_paise", 0)
    if type(od) is not int or od < 0 or od > ledger.MAX_AMOUNT:
        raise ApiError(400, "invalid_overdraft", "overdraft_limit_paise must be an integer >= 0")
    for _ in range(10):
        number = "LL" + "".join(secrets.choice("0123456789") for _ in range(10))
        try:
            g.conn.execute("INSERT INTO accounts(number,name,kind,owner_id,overdraft_limit,created_at) VALUES(?,?,?,?,?,?)",
                           (number, name, "customer", owner["id"], od, now()))
            break
        except sqlite3.IntegrityError:
            continue
    else:
        raise ApiError(500, "internal", "could not allocate account number")
    _security_event(g.conn, g.user, "admin_create_account", f"{number} owner={owner['username']} overdraft={od}")
    a = get_account(number)
    return jsonify(acct_json(a, a["owner"])), 201


@bp.patch("/accounts/<number>")
@auth.require("admin")
def adjust_account(number):
    """Admin may freeze/unfreeze and set the overdraft limit. It can never touch a balance."""
    d = body()
    g.conn.execute("BEGIN IMMEDIATE")
    try:
        a = g.conn.execute("SELECT * FROM accounts WHERE number=? AND kind='customer'", (number,)).fetchone()
        if not a:
            raise ApiError(404, "account_not_found", "account not found")
        frozen, od = a["frozen"], a["overdraft_limit"]
        if "frozen" in d:
            if type(d["frozen"]) is not bool:
                raise ApiError(400, "invalid_frozen", "frozen must be true/false")
            frozen = int(d["frozen"])
        if "overdraft_limit_paise" in d:
            v = d["overdraft_limit_paise"]
            if type(v) is not int or v < 0 or v > ledger.MAX_AMOUNT:
                raise ApiError(400, "invalid_overdraft", "overdraft_limit_paise must be an integer >= 0")
            if a["balance"] < -v:
                raise ApiError(409, "overdraft_below_balance", "account is already overdrawn beyond that limit")
            od = v
        g.conn.execute("UPDATE accounts SET frozen=?, overdraft_limit=? WHERE id=?", (frozen, od, a["id"]))
        _security_event(g.conn, g.user, "admin_adjust_account", f"{number} frozen={frozen} overdraft={od}")
        g.conn.execute("COMMIT")
    except BaseException:
        if g.conn.in_transaction:
            g.conn.execute("ROLLBACK")
        raise
    a = get_account(number)
    return jsonify(acct_json(a, a["owner"]))


@bp.get("/accounts")
@auth.require("admin", "holder", "auditor")
def list_accounts():
    if g.user["role"] == "holder":
        rows = g.conn.execute("SELECT a.*, u.username owner FROM accounts a JOIN users u ON u.id=a.owner_id "
                              "WHERE a.kind='customer' AND a.owner_id=? ORDER BY a.id", (g.user["id"],)).fetchall()
    else:
        rows = g.conn.execute("SELECT a.*, u.username owner FROM accounts a JOIN users u ON u.id=a.owner_id "
                              "WHERE a.kind='customer' ORDER BY a.id").fetchall()
    return jsonify([acct_json(r, r["owner"]) for r in rows])


@bp.get("/accounts/<number>")
@auth.require("admin", "holder", "auditor")
def account(number):
    a = get_account(number)
    return jsonify(acct_json(a, a["owner"]))


@bp.get("/accounts/<number>/statement")
@auth.require("admin", "holder", "auditor")
def account_statement(number):
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 500)
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        raise ApiError(400, "invalid_paging", "limit/offset must be integers")
    a = get_account(number)
    return jsonify({"account": acct_json(a, a["owner"]), "entries": ledger.statement(g.conn, a["id"], limit, offset)})


# ------------------------------------------------------------------ money
def _money(kind):
    d = body()
    key = request.headers.get("Idempotency-Key")
    if kind == "deposit":
        src, dst = None, d.get("account")
    elif kind == "withdrawal":
        src, dst = d.get("account"), None
    else:
        src, dst = d.get("from_account"), d.get("to_account")
    status, out, replayed = ledger.post_money(g.conn, g.user, kind, src, dst, d.get("amount_paise"), key)
    resp = jsonify(out)
    resp.status_code = status
    if replayed:
        resp.headers["Idempotent-Replayed"] = "true"
    return resp


@bp.post("/deposits")
@auth.require("admin", "holder", "auditor")   # role check for deposit happens inside the engine, under the lock
def deposit():
    return _money("deposit")


@bp.post("/withdrawals")
@auth.require("admin", "holder", "auditor")
def withdraw():
    return _money("withdrawal")


@bp.post("/transfers")
@auth.require("admin", "holder", "auditor")
def transfer():
    return _money("transfer")


# ------------------------------------------------------------------ audit (admin + auditor, read-only)
@bp.get("/audit/verify")
@auth.require("admin", "auditor")
def verify():
    return jsonify(ledger.verify_ledger(g.conn))


@bp.get("/audit/journal")
@auth.require("admin", "auditor")
def journal():
    limit = min(max(request.args.get("limit", 100, type=int), 1), 500)
    offset = max(request.args.get("offset", 0, type=int), 0)
    only = request.args.get("filter")
    where = {"flagged": "WHERE j.flagged=1", "refused": "WHERE j.status='refused'"}.get(only, "")
    rows = g.conn.execute(f"""SELECT j.id, j.txn_id, j.type, j.status, j.reason, j.amount, j.flagged, j.flag_reason,
        j.created_at, j.idem_key, u.username initiated_by, fa.number from_account, ta.number to_account, j.row_hash
        FROM journal j JOIN users u ON u.id=j.user_id JOIN accounts fa ON fa.id=j.from_account_id
        JOIN accounts ta ON ta.id=j.to_account_id {where} ORDER BY j.id DESC LIMIT ? OFFSET ?""", (limit, offset)).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.get("/audit/replays")
@auth.require("admin", "auditor")
def replays():
    rows = g.conn.execute("""SELECT r.id, j.txn_id, r.idem_key, r.same_request, r.created_at, u.username
        FROM replays r JOIN journal j ON j.id=r.journal_id JOIN users u ON u.id=r.user_id
        ORDER BY r.id DESC LIMIT 500""").fetchall()
    return jsonify([dict(r) for r in rows])


@bp.get("/audit/security-events")
@auth.require("admin", "auditor")
def security_events():
    rows = g.conn.execute("SELECT * FROM security_events ORDER BY id DESC LIMIT 500").fetchall()
    return jsonify([dict(r) for r in rows])


@bp.get("/health")
def health():
    g.conn.execute("SELECT 1")
    return jsonify({"ok": True})
