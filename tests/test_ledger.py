"""Conservation, statements, validation."""
import random
import uuid

import pytest


def k():
    return "k-" + uuid.uuid4().hex


def test_conservation_over_random_sequential_operations(env):
    rnd = random.Random(42)
    accts = {"alice": env.alice, "bob": env.bob, "vendor": env.vendor}
    ext_in = ext_out = 0
    base = sum(env.balance(a) for a in accts.values())
    for _ in range(200):
        who, num = rnd.choice(list(accts.items()))
        op = rnd.choice(["transfer", "transfer", "withdraw", "deposit"])
        amt = rnd.randint(1, 300_000)
        if op == "transfer":
            to = rnd.choice([a for a in accts.values() if a != num])
            env.transfer(who, num, to, amt, k())
        elif op == "withdraw":
            r = env.withdraw(who, num, amt, k()); ext_out += amt if r.status_code == 201 else 0
        else:
            r = env.deposit("admin", num, amt, k()); ext_in += amt if r.status_code == 201 else 0
    assert sum(env.balance(a) for a in accts.values()) == base + ext_in - ext_out      # only external flows change the total
    v = env.verify()
    assert v["ok"], v["problems"]
    assert v["stats"]["sum_all_balances_paise"] == 0                    # including the external counterparty
    assert all(env.balance(a) >= 0 for a in accts.values())


def test_every_posted_journal_row_nets_to_zero(env):
    for _ in range(20):
        env.transfer("alice", env.alice, env.bob, 1_111, k())
    c = env.raw()
    bad = c.execute("SELECT j.id FROM journal j JOIN entries e ON e.journal_id=j.id WHERE j.status='posted' "
                    "GROUP BY j.id HAVING SUM(e.delta)<>0").fetchall()
    assert bad == []
    c.close()


def test_statement_has_debits_credits_refusals_running_balance_and_initiator(env):
    env.transfer("alice", env.alice, env.vendor, 100_000, k())
    env.transfer("alice", env.alice, env.vendor, 99_999_999, k())      # refused
    env.deposit("admin", env.alice, 50_000, k())
    st = env.call("alice", "GET", f"/api/accounts/{env.alice}/statement").json["entries"]
    kinds = [(e["type"], e["status"]) for e in st]
    assert kinds == [("deposit", "posted"), ("transfer", "posted"), ("transfer", "refused"), ("deposit", "posted")]
    assert [e["balance_after"] for e in st] == [1_000_000, 900_000, 900_000, 950_000]
    assert st[1]["debit_paise"] == 100_000 and st[3]["credit_paise"] == 50_000
    assert st[2]["reason"] == "insufficient_funds" and st[2]["debit_paise"] is None
    assert [e["initiated_by"] for e in st] == ["admin", "alice", "alice", "admin"]
    # running balance is the sum of the postings
    assert st[-1]["balance_after"] == sum((e["credit_paise"] or 0) - (e["debit_paise"] or 0) for e in st)


def test_refused_attempt_is_visible_to_initiator_not_counterparty(env):
    env.transfer("vendor", env.vendor, env.bob, 5_000, k())           # refused, vendor has 0
    bob_st = env.call("bob", "GET", f"/api/accounts/{env.bob}/statement").json["entries"]
    assert all(e["status"] == "posted" for e in bob_st)
    ven_st = env.call("vendor", "GET", f"/api/accounts/{env.vendor}/statement").json["entries"]
    assert [e["status"] for e in ven_st] == ["refused"]


def test_statement_paging(env):
    for _ in range(7):
        env.transfer("alice", env.alice, env.bob, 100, k())
    st = env.call("alice", "GET", f"/api/accounts/{env.alice}/statement?limit=3&offset=3").json["entries"]
    assert len(st) == 3 and [e["balance_after"] for e in st] == [999_700, 999_600, 999_500]


@pytest.mark.parametrize("amount", [0, -5, 1.5, "100", True, None, 10**12, 10**30, [1]])
def test_invalid_amounts_rejected_without_side_effects(env, amount):
    a0 = env.balance(env.alice)
    r = env.call("alice", "POST", "/api/transfers", {"from_account": env.alice, "to_account": env.bob, "amount_paise": amount}, k())
    assert r.status_code == 400
    assert env.balance(env.alice) == a0
    c = env.raw(); assert c.execute("SELECT COUNT(*) FROM journal WHERE user_id=(SELECT id FROM users WHERE username='alice')").fetchone()[0] == 0


def test_self_transfer_unknown_destination_and_external_are_rejected(env):
    assert env.transfer("alice", env.alice, env.alice, 100, k()).status_code == 400
    assert env.transfer("alice", env.alice, "LL9999999999", 100, k()).status_code == 404
    assert env.transfer("alice", env.alice, "EXTERNAL", 100, k()).status_code == 404       # cannot address the world account
    assert env.transfer("alice", "EXTERNAL", env.alice, 100, k()).status_code == 403
    assert env.deposit("admin", "EXTERNAL", 100, k()).status_code == 404
    assert env.verify()["ok"]


def test_large_and_rapid_movements_are_flagged_but_still_post(env):
    r = env.deposit("admin", env.alice, 6_000_000, k())
    assert r.status_code == 201 and r.json["flagged"] is True
    flagged = env.call("auditor", "GET", "/api/audit/journal?filter=flagged").json
    assert any(f["flag_reason"] and "large_amount" in f["flag_reason"] for f in flagged)


def test_malformed_bodies_do_not_500(env):
    h = {"Authorization": "Bearer " + env.token("alice"), "Idempotency-Key": k()}
    c = env.client()
    assert c.post("/api/transfers", data="not json", headers=h).status_code == 400
    assert c.post("/api/transfers", json=[1, 2], headers=h).status_code == 400
    assert c.post("/api/transfers", json={"from_account": 5, "to_account": {}, "amount_paise": 1}, headers=h).status_code == 400
