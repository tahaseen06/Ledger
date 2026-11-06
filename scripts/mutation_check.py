"""Mutation check: remove ONE lock/check from a scratch copy of the code and make sure the tests FAIL.
If a mutated copy still passes the suite, that guard is untested. Usage: python -m scripts.mutation_check
"""
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MUTATIONS = [
    ("write lock removed (BEGIN IMMEDIATE -> BEGIN)", "app/ledger.py",
     'conn.execute("BEGIN IMMEDIATE")           # <- the write lock', 'conn.execute("BEGIN")           # <- the write lock'),
    ("app-level balance check removed", "app/ledger.py",
     'elif src and src["balance"] - amount < -src["overdraft_limit"]:', "elif False:"),
    ("idempotency lookup disabled", "app/ledger.py", "    if prior:\n", "    if False:\n"),
    ("idempotency payload comparison disabled", "app/ledger.py",
     'same = prior["request_hash"] == req_hash', "same = True"),
    ("replay log not written", "app/ledger.py",
     'conn.execute("INSERT INTO replays(', '(lambda *a: None)("INSERT INTO replays('),
    ("ownership check removed", "app/ledger.py", 'src["owner_id"] == actor["id"]', "True"),
    ("holders may deposit", "app/ledger.py",
     '        return role == "admin"\n    if kind == "withdrawal"', '        return True\n    if kind == "withdrawal"'),
    ("frozen check removed", "app/ledger.py",
     'if (src and src["frozen"]) or (dst and dst["frozen"]):', "if False:"),
    ("hash verification disabled", "app/ledger.py",
     'if row_hash(r["prev_hash"], journal_fields(r), ents) != r["row_hash"]:', "if False:"),
    ("journal append-only trigger removed", "app/schema.sql",
     "CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal\nBEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;", ""),
    ("route role check removed", "app/auth.py", 'if g.user["role"] not in roles:', "if False:"),
    ("both floors removed (app check AND db CHECK)", "app/ledger.py",
     'elif src and src["balance"] - amount < -src["overdraft_limit"]:', "elif False:"),
]
EXTRA = {"both floors removed (app check AND db CHECK)": ("app/schema.sql",
         "CHECK (kind = 'external' OR balance >= -overdraft_limit),", "")}


def run(m):
    name, rel, old, new = m
    d = tempfile.mkdtemp(prefix="mut-")
    try:
        shutil.copytree(ROOT, d, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".git", "*.db*", "__pycache__", ".pytest_cache"))
        for r, o, n in [(rel, old, new)] + ([EXTRA[name]] if name in EXTRA else []):
            p = os.path.join(d, r); s = open(p).read()
            if o not in s:
                return name, "PATTERN NOT FOUND (update the mutation)"
            open(p, "w").write(s.replace(o, n, 1))
        r = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "tests"], cwd=d, capture_output=True, text=True, timeout=900)
        return name, "caught" if r.returncode != 0 else "SURVIVED (tests did not notice!)"
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    with ThreadPoolExecutor(3) as ex:
        results = list(ex.map(run, MUTATIONS))
    bad = 0
    for name, verdict in results:
        print(f"{verdict:34s} {name}")
        bad += verdict != "caught"
    sys.exit(1 if bad else 0)
