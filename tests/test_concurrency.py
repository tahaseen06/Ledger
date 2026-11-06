"""Races. Every test fires requests at the same instant with a barrier and checks the money afterwards."""
import uuid


def k():
    return "k-" + uuid.uuid4().hex


def test_concurrent_withdrawals_cannot_overdraw(env):
    start = env.balance(env.alice)                       # 1,000,000 paise
    amt = 300_000
    res = env.race([lambda: env.withdraw("alice", env.alice, amt, k()) for _ in range(20)])
    codes = sorted(r.status_code for r in res)
    assert codes.count(201) == 3 and codes.count(422) == 17, codes   # only 3 x 300k fit in 1,000k
    assert all(r.status_code != 503 and r.status_code < 500 for r in res)
    assert env.balance(env.alice) == start - 3 * amt >= 0
    assert env.verify()["ok"]


def test_concurrent_transfers_to_different_payees_cannot_double_spend(env):
    start = env.balance(env.alice)
    dests = [env.bob, env.vendor] * 6
    res = env.race([lambda d=d: env.transfer("alice", env.alice, d, 600_000, k()) for d in dests])
    ok = [r for r in res if r.status_code == 201]
    assert len(ok) == 1                                   # 1,000k only covers one 600k
    assert env.balance(env.alice) == start - 600_000
    v = env.verify(); assert v["ok"], v["problems"]


def test_opposing_transfers_do_not_deadlock_or_lose_money(env):
    a0, b0 = env.balance(env.alice), env.balance(env.bob)
    jobs = []
    for _ in range(10):
        jobs.append(lambda: env.transfer("alice", env.alice, env.bob, 10_000, k()))
        jobs.append(lambda: env.transfer("bob", env.bob, env.alice, 10_000, k()))
    res = env.race(jobs)
    assert all(r.status_code == 201 for r in res), [r.json for r in res if r.status_code != 201]
    assert env.balance(env.alice) + env.balance(env.bob) == a0 + b0
    assert env.balance(env.alice) == a0 and env.balance(env.bob) == b0   # 10 each way nets out
    assert env.verify()["ok"]


def test_overdraft_limit_is_enforced_under_race(env):
    r = env.call("admin", "PATCH", f"/api/accounts/{env.bob}", {"overdraft_limit_paise": 200_000})
    assert r.status_code == 200
    start = env.balance(env.bob)                          # 500,000 ; floor = -200,000 ; 700,000 spendable
    res = env.race([lambda: env.transfer("bob", env.bob, env.vendor, 100_000, k()) for _ in range(15)])
    assert sum(r.status_code == 201 for r in res) == 7
    assert env.balance(env.bob) == start - 700_000 == -200_000
    assert env.verify()["ok"]


def test_mixed_random_load_conserves_money(env):
    import random
    rnd = random.Random(7)
    accts = [env.alice, env.bob, env.vendor]
    owner = {env.alice: "alice", env.bob: "bob", env.vendor: "vendor"}
    total_before = sum(env.balance(a) for a in accts)
    jobs = []
    for _ in range(60):
        s, d = rnd.sample(accts, 2)
        jobs.append(lambda s=s, d=d, a=rnd.randint(1, 400_000): env.transfer(owner[s], s, d, a, k()))
    for _ in range(10):
        jobs.append(lambda a=rnd.randint(1, 100_000): env.deposit("admin", env.vendor, a, k()))
    res = env.race(jobs)
    assert all(r.status_code in (201, 422) for r in res), [r.status_code for r in res]
    deposits = sum(r.json["amount_paise"] for r in res if r.status_code == 201 and r.json["type"] == "deposit")
    assert sum(env.balance(a) for a in accts) == total_before + deposits
    assert all(env.balance(a) >= 0 for a in accts)
    v = env.verify(); assert v["ok"], v["problems"]
    assert v["stats"]["sum_all_balances_paise"] == 0


def test_busy_database_never_leaves_partial_state(env):
    """A contending writer holding the lock makes us wait, not corrupt."""
    import threading, time
    import sqlite3
    conn = sqlite3.connect(env.path, isolation_level=None, check_same_thread=False); conn.execute("BEGIN IMMEDIATE")
    t = threading.Timer(0.5, lambda: (conn.execute("ROLLBACK"), conn.close())); t.start()
    t0 = time.time()
    r = env.transfer("alice", env.alice, env.bob, 1_000, k())
    assert r.status_code == 201 and time.time() - t0 >= 0.4
    t.join()
    assert env.verify()["ok"]
