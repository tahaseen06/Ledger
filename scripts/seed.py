"""Seed demo users/accounts. Idempotent. Demo passwords can be overridden with LEDGERLINE_DEMO_PASSWORD.
Usage: python -m scripts.seed
"""
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import auth, db, ledger  # noqa: E402

PW = os.environ.get("LEDGERLINE_DEMO_PASSWORD", "ChangeMe-Demo-1")
USERS = [("admin", "admin"), ("alice", "holder"), ("bob", "holder"), ("vendor", "holder"), ("auditor", "auditor")]


def seed(path=None):
    db.init_db(path)
    conn = db.connect(path)
    created = {}
    for name, role in USERS:
        if not conn.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
            conn.execute("INSERT INTO users(username,pw_hash,role,created_at) VALUES(?,?,?,?)",
                         (name, auth.hash_password(PW), role, ledger.now()))
    admin = conn.execute("SELECT * FROM users WHERE username='admin'").fetchone()
    for owner, label, od in (("alice", "Alice Main", 0), ("bob", "Bob Main", 0), ("vendor", "Vendor Co", 0)):
        u = conn.execute("SELECT * FROM users WHERE username=?", (owner,)).fetchone()
        a = conn.execute("SELECT * FROM accounts WHERE owner_id=? AND name=?", (u["id"], label)).fetchone()
        if not a:
            number = "LL" + "".join(secrets.choice("0123456789") for _ in range(10))
            conn.execute("INSERT INTO accounts(number,name,kind,owner_id,overdraft_limit,created_at) VALUES(?,?,?,?,?,?)",
                         (number, label, "customer", u["id"], od, ledger.now()))
            a = conn.execute("SELECT * FROM accounts WHERE number=?", (number,)).fetchone()
            amount = {"alice": 1_000_000, "bob": 500_000, "vendor": 0}[owner]
            if amount:   # opening funds go through the real engine, so the ledger is consistent from row 1
                ledger.post_money(conn, admin, "deposit", None, number, amount, f"seed-opening-{owner}")
        created[owner] = a["number"]
    conn.close()
    return created


if __name__ == "__main__":
    for owner, number in seed().items():
        print(f"{owner:8s} account {number}")
    print("Users: admin / alice / bob / vendor / auditor   password:", PW)
