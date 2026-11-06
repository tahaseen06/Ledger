import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db  # noqa: E402
from scripts.seed import PW, seed  # noqa: E402


class Env:
    def __init__(self, app, accounts):
        self.app, self.accounts, self.path = app, accounts, app.config["DB_PATH"]
        self._tokens = {}

    def client(self):
        return self.app.test_client()

    def token(self, username):
        if username not in self._tokens:
            r = self.client().post("/api/auth/login", json={"username": username, "password": PW})
            assert r.status_code == 200, r.json
            self._tokens[username] = r.json["token"]
        return self._tokens[username]

    def call(self, user, method, path, json=None, key=None, client=None):
        h = {"Authorization": "Bearer " + self.token(user)}
        if key:
            h["Idempotency-Key"] = key
        return (client or self.client()).open(path, method=method, json=json, headers=h)

    def transfer(self, user, src, dst, amount, key, client=None):
        return self.call(user, "POST", "/api/transfers", {"from_account": src, "to_account": dst, "amount_paise": amount}, key, client)

    def withdraw(self, user, acct, amount, key, client=None):
        return self.call(user, "POST", "/api/withdrawals", {"account": acct, "amount_paise": amount}, key, client)

    def deposit(self, user, acct, amount, key, client=None):
        return self.call(user, "POST", "/api/deposits", {"account": acct, "amount_paise": amount}, key, client)

    def balance(self, number):
        return self.call("admin", "GET", f"/api/accounts/{number}").json["balance_paise"]

    def verify(self):
        r = self.call("auditor", "GET", "/api/audit/verify")
        assert r.status_code == 200
        return r.json

    def raw(self):
        return db.connect(self.path)

    def race(self, jobs):
        """Run callables at the same instant (barrier) and return their results in order."""
        barrier, out = threading.Barrier(len(jobs)), [None] * len(jobs)

        def run(i, fn):
            barrier.wait()
            try:
                out[i] = fn()
            except Exception as e:  # surface thread errors as test failures
                out[i] = e

        ts = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(jobs)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        for o in out:
            assert not isinstance(o, Exception), repr(o)
        return out


@pytest.fixture()
def env(tmp_path):
    path = str(tmp_path / "test.db")
    accounts = seed(path)          # alice 10,000.00 / bob 5,000.00 / vendor 0
    app = create_app(path)
    e = Env(app, accounts)
    e.alice, e.bob, e.vendor = accounts["alice"], accounts["bob"], accounts["vendor"]
    return e
