"""The money engine. Everything that moves money goes through post_money().

Concurrency model
-----------------
Every money-moving request runs inside ONE `BEGIN IMMEDIATE` transaction. SQLite allows a single
writer at a time, so IMMEDIATE takes the database write lock *before* anything is read. That means:
  * the idempotency lookup, the ownership check, the balance check, the entries, the balance update
    and the journal row all happen under the same lock - no check-then-act gap;
  * a retry that arrives while the first attempt is still in flight simply waits for the lock, then
    finds the committed result and replays it;
  * if anything fails before COMMIT, ROLLBACK leaves no trace: no entries, no balance change, no key.
The database enforces the same invariants again with CHECK constraints and triggers (schema.sql).
"""
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from .db import external_id

MAX_AMOUNT = 1_000_000_000          # 1 crore INR in paise; also keeps us far away from int64 limits
FLAG_LARGE = 5_000_000              # >= INR 50,000 is flagged for review
FLAG_VELOCITY = 5                   # >= 5 money moves by one user in 10 minutes is flagged
KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
GENESIS = "GENESIS"


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _fail_point(name):  # noqa: D401 - test seam: tests patch this to simulate a crash mid-transaction
    """No-op in production."""


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def row_hash(prev_hash, fields, entries):
    return hashlib.sha256((prev_hash + _canon({"f": fields, "e": entries})).encode()).hexdigest()


def journal_fields(r):
    """The exact set of journal columns covered by the hash chain."""
    return {k: r[k] for k in (
        "txn_id", "user_id", "idem_key", "request_hash", "type", "from_account_id", "to_account_id",
        "amount", "status", "reason", "flagged", "flag_reason", "http_status", "response_json", "created_at")}


# --------------------------------------------------------------------------- validation
def validate_amount(v):
    if type(v) is not int:  # rejects floats, strings, bools
        raise ApiError(400, "invalid_amount", "amount_paise must be an integer number of paise")
    if v <= 0 or v > MAX_AMOUNT:
        raise ApiError(400, "invalid_amount", f"amount_paise must be between 1 and {MAX_AMOUNT}")
    return v


def validate_key(k):
    if not isinstance(k, str) or not KEY_RE.match(k):
        raise ApiError(400, "invalid_idempotency_key",
                       "Idempotency-Key header is required: 8-128 chars of A-Z a-z 0-9 _ . : -")
    return k


def validate_number(v, field):
    if not isinstance(v, str) or not (1 <= len(v) <= 40):
        raise ApiError(400, "invalid_account", f"{field} must be an account number string")
    return v


# --------------------------------------------------------------------------- authorisation
def _authorize(actor, kind, src, dst):
    """Decided by the server, per request, from the database state read under the write lock."""
    role = actor["role"]
    if role == "auditor":
        return False
    if kind == "deposit":      # cash-in is an operator action; holders cannot mint money
        return role == "admin"
    if kind == "withdrawal":   # operator adjustment, or the owner taking money out
        return role == "admin" or (role == "holder" and src["owner_id"] == actor["id"])
    if kind == "transfer":     # only the owner moves their own money; admins cannot move customer funds
        return role == "holder" and src["owner_id"] == actor["id"]
    return False


def _security_event(conn, actor, action, detail):
    conn.execute("INSERT INTO security_events(user_id,username,action,detail,created_at) VALUES(?,?,?,?,?)",
                 (actor["id"] if actor else None, actor["username"] if actor else None, action, detail, now()))


# --------------------------------------------------------------------------- the engine
def post_money(conn, actor, kind, from_no, to_no, amount, key):
    """Returns (http_status, body_dict, replayed_bool). Raises ApiError only for nothing-happened cases."""
    validate_key(key)
    validate_amount(amount)
    if kind == "deposit":
        validate_number(to_no, "account"); from_no = None
    elif kind == "withdrawal":
        validate_number(from_no, "account"); to_no = None
    else:
        validate_number(from_no, "from_account"); validate_number(to_no, "to_account")
        if from_no == to_no:
            raise ApiError(400, "same_account", "cannot transfer to the same account")

    req_hash = hashlib.sha256(_canon([kind, from_no, to_no, amount]).encode()).hexdigest()

    conn.execute("BEGIN IMMEDIATE")           # <- the write lock; everything below is serialised
    try:
        result = _post_locked(conn, actor, kind, from_no, to_no, amount, key, req_hash)
        conn.execute("COMMIT")
        return result
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _post_locked(conn, actor, kind, from_no, to_no, amount, key, req_hash):
    # 1. Idempotency: has this user already used this key?
    prior = conn.execute("SELECT * FROM journal WHERE user_id=? AND idem_key=?", (actor["id"], key)).fetchone()
    if prior:
        same = prior["request_hash"] == req_hash
        conn.execute("INSERT INTO replays(journal_id,user_id,idem_key,same_request,created_at) VALUES(?,?,?,?,?)",
                     (prior["id"], actor["id"], key, int(same), now()))
        if not same:
            return 422, {"error": "idempotency_key_reuse",
                         "message": "this Idempotency-Key was already used with a different request"}, True
        return prior["http_status"], json.loads(prior["response_json"]), True

    ext = external_id(conn)

    def load(number):
        return conn.execute("SELECT * FROM accounts WHERE number=? AND kind='customer'", (number,)).fetchone()

    src = load(from_no) if from_no else None
    dst = load(to_no) if to_no else None

    # 2. Authorisation. The source-side lookup failing and "not yours" are indistinguishable to the caller.
    if kind != "deposit" and src is None:
        _security_event(conn, actor, "denied", f"{kind}: source account unknown or not permitted")
        return 403, {"error": "forbidden", "message": "not permitted"}, False
    if not _authorize(actor, kind, src, dst):
        _security_event(conn, actor, "denied", f"{kind}: from={from_no} to={to_no} amount={amount}")
        return 403, {"error": "forbidden", "message": "not permitted"}, False
    if kind == "deposit" and dst is None:
        raise ApiError(404, "account_not_found", "account not found")
    if kind == "transfer" and dst is None:
        raise ApiError(404, "account_not_found", "destination account not found")

    # 3. Business rules -> a refusal is a recorded outcome, not an exception.
    reason = None
    if (src and src["frozen"]) or (dst and dst["frozen"]):
        reason = "account_frozen"
    elif src and src["balance"] - amount < -src["overdraft_limit"]:
        reason = "insufficient_funds"

    src_id = src["id"] if src else ext
    dst_id = dst["id"] if dst else ext
    txn_id = uuid.uuid4().hex
    created = now()

    # 4. Post.
    entries = []
    if reason is None:
        for acct_id, delta in ((src_id, -amount), (dst_id, amount)):
            if acct_id == ext:
                bal_before = conn.execute("SELECT balance FROM accounts WHERE id=?", (acct_id,)).fetchone()["balance"]
            else:
                bal_before = (src if acct_id == src_id else dst)["balance"]
            entries.append((acct_id, delta, bal_before + delta))
        flagged, flag_reason = _flags(conn, actor, amount, created)
        # Only the caller's own side of the movement is reported (never the counterparty's balance).
        mine, bal = (to_no, entries[1][2]) if kind == "deposit" else (from_no, entries[0][2])
        body = {"status": "posted", "txn_id": txn_id, "type": kind, "amount_paise": amount,
                "from_account": from_no, "to_account": to_no, "flagged": bool(flagged),
                "account": mine, "balance_after_paise": bal}
        status, http = "posted", 201
    else:
        flagged, flag_reason = 0, None
        body = {"status": "refused", "txn_id": txn_id, "type": kind, "amount_paise": amount,
                "from_account": from_no, "to_account": to_no, "reason": reason}
        status, http = "refused", 422

    prev = conn.execute("SELECT row_hash FROM journal ORDER BY id DESC LIMIT 1").fetchone()
    prev_hash = prev["row_hash"] if prev else GENESIS
    fields = {"txn_id": txn_id, "user_id": actor["id"], "idem_key": key, "request_hash": req_hash,
              "type": kind, "from_account_id": src_id, "to_account_id": dst_id, "amount": amount,
              "status": status, "reason": reason, "flagged": flagged, "flag_reason": flag_reason,
              "http_status": http, "response_json": _canon(body), "created_at": created}
    h = row_hash(prev_hash, fields, [list(e) for e in entries])

    cur = conn.execute(
        "INSERT INTO journal(txn_id,user_id,idem_key,request_hash,type,from_account_id,to_account_id,amount,"
        "status,reason,flagged,flag_reason,http_status,response_json,created_at,prev_hash,row_hash) "
        "VALUES(:txn_id,:user_id,:idem_key,:request_hash,:type,:from_account_id,:to_account_id,:amount,"
        ":status,:reason,:flagged,:flag_reason,:http_status,:response_json,:created_at,:prev_hash,:row_hash)",
        {**fields, "prev_hash": prev_hash, "row_hash": h})
    jid = cur.lastrowid
    _fail_point("after_journal")

    for acct_id, delta, bal_after in entries:
        conn.execute("INSERT INTO entries(journal_id,account_id,delta,balance_after) VALUES(?,?,?,?)",
                     (jid, acct_id, delta, bal_after))
    _fail_point("after_entries")

    for acct_id, delta, bal_after in entries:
        conn.execute("UPDATE accounts SET balance=? WHERE id=?", (bal_after, acct_id))
    _fail_point("after_balances")

    return http, body, False


def _flags(conn, actor, amount, created):
    """Rule-based review flags. They never block money; a human reviews them (see /api/audit/flagged)."""
    reasons = []
    if amount >= FLAG_LARGE:
        reasons.append("large_amount")
    since = datetime.fromisoformat(created).timestamp() - 600
    recent = conn.execute("SELECT COUNT(*) c FROM journal WHERE user_id=? AND status='posted' AND created_at>=?",
                          (actor["id"], datetime.fromtimestamp(since, timezone.utc).isoformat(timespec="microseconds"))
                          ).fetchone()["c"]
    if recent + 1 >= FLAG_VELOCITY:
        reasons.append("high_velocity")
    return (1, ",".join(reasons)) if reasons else (0, None)


# --------------------------------------------------------------------------- reads
def statement(conn, account_id, limit=100, offset=0):
    rows = conn.execute("""
      SELECT j.txn_id, j.type, j.status, j.reason, j.amount, j.flagged, j.created_at, u.username AS initiated_by,
             fa.number AS from_account, ta.number AS to_account, e.delta,
             CASE WHEN e.id IS NOT NULL THEN e.balance_after
                  ELSE COALESCE((SELECT balance_after FROM entries x WHERE x.account_id=:a AND x.journal_id<j.id
                                 ORDER BY x.id DESC LIMIT 1),0) END AS balance_after
      FROM journal j
      JOIN users u ON u.id=j.user_id
      JOIN accounts fa ON fa.id=j.from_account_id
      JOIN accounts ta ON ta.id=j.to_account_id
      LEFT JOIN entries e ON e.journal_id=j.id AND e.account_id=:a
      WHERE e.id IS NOT NULL
         OR (j.status='refused' AND ((j.type='deposit' AND j.to_account_id=:a)
                                  OR (j.type<>'deposit' AND j.from_account_id=:a)))
      ORDER BY j.id LIMIT :l OFFSET :o""", {"a": account_id, "l": limit, "o": offset}).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        delta = r["delta"]
        if delta is not None:                       # posted: counterparty is the other side of my entry
            d["counterparty"] = r["to_account"] if delta < 0 else r["from_account"]
        else:                                       # refused: the statement owner is the initiating side
            d["counterparty"] = r["from_account"] if r["type"] == "deposit" else r["to_account"]
        d["debit_paise"] = -delta if delta is not None and delta < 0 else None
        d["credit_paise"] = delta if delta is not None and delta > 0 else None
        del d["delta"]
        out.append(d)
    return out


def verify_ledger(conn):
    """Re-derive everything from the append-only tables and report any disagreement."""
    problems, stats = [], {}
    conn.execute("BEGIN")  # one consistent snapshot (WAL)
    try:
        prev = GENESIS
        n_j = 0
        entries_by_j = {}
        for e in conn.execute("SELECT journal_id,account_id,delta,balance_after FROM entries ORDER BY id"):
            entries_by_j.setdefault(e["journal_id"], []).append([e["account_id"], e["delta"], e["balance_after"]])
        for r in conn.execute("SELECT * FROM journal ORDER BY id"):
            n_j += 1
            ents = entries_by_j.get(r["id"], [])
            if r["prev_hash"] != prev:
                problems.append(f"journal {r['id']}: chain broken (prev_hash mismatch)")
            if row_hash(r["prev_hash"], journal_fields(r), ents) != r["row_hash"]:
                problems.append(f"journal {r['id']}: content does not match its hash (row or entries altered)")
            prev = r["row_hash"]
            if r["status"] == "posted":
                if sum(e[1] for e in ents) != 0 or len(ents) < 2:
                    problems.append(f"journal {r['id']}: entries do not net to zero")
            elif ents:
                problems.append(f"journal {r['id']}: refused attempt has postings")
        orphan = set(entries_by_j) - {r["id"] for r in conn.execute("SELECT id FROM journal")}
        if orphan:
            problems.append(f"entries reference missing journal rows: {sorted(orphan)}")

        total = 0
        for a in conn.execute("SELECT * FROM accounts"):
            s = conn.execute("SELECT COALESCE(SUM(delta),0) s FROM entries WHERE account_id=?", (a["id"],)).fetchone()["s"]
            last = conn.execute("SELECT balance_after FROM entries WHERE account_id=? ORDER BY id DESC LIMIT 1",
                                (a["id"],)).fetchone()
            last = last["balance_after"] if last else 0
            if not (a["balance"] == s == last):
                problems.append(f"account {a['number']}: balance {a['balance']} != ledger sum {s} / last {last}")
            if a["kind"] == "customer" and a["balance"] < -a["overdraft_limit"]:
                problems.append(f"account {a['number']}: below its floor")
            total += a["balance"]
        if total != 0:
            problems.append(f"sum of all balances is {total}, expected 0")

        dep = conn.execute("SELECT COALESCE(SUM(amount),0) s FROM journal WHERE status='posted' AND type='deposit'").fetchone()["s"]
        wd = conn.execute("SELECT COALESCE(SUM(amount),0) s FROM journal WHERE status='posted' AND type='withdrawal'").fetchone()["s"]
        cust = conn.execute("SELECT COALESCE(SUM(balance),0) s FROM accounts WHERE kind='customer'").fetchone()["s"]
        if cust != dep - wd:
            problems.append(f"customer balances {cust} != deposits {dep} - withdrawals {wd}")
        stats = {
            "journal_rows": n_j, "head_hash": prev,
            "posted": conn.execute("SELECT COUNT(*) c FROM journal WHERE status='posted'").fetchone()["c"],
            "refused": conn.execute("SELECT COUNT(*) c FROM journal WHERE status='refused'").fetchone()["c"],
            "duplicate_requests_replayed": conn.execute("SELECT COUNT(*) c FROM replays WHERE same_request=1").fetchone()["c"],
            "key_misuse_attempts": conn.execute("SELECT COUNT(*) c FROM replays WHERE same_request=0").fetchone()["c"],
            "total_deposits_paise": dep, "total_withdrawals_paise": wd,
            "sum_customer_balances_paise": cust, "sum_all_balances_paise": total,
        }
    finally:
        conn.execute("COMMIT")
    return {"ok": not problems, "problems": problems, "stats": stats}
