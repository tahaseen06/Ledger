import uuid


def k():
    return "k-" + uuid.uuid4().hex


def count(env, sql, *a):
    c = env.raw(); n = c.execute(sql, a).fetchone()[0]; c.close(); return n


def test_replay_returns_first_result_and_moves_money_once(env):
    key = k()
    a0, v0 = env.balance(env.alice), env.balance(env.vendor)
    r1 = env.transfer("alice", env.alice, env.vendor, 250_000, key)
    r2 = env.transfer("alice", env.alice, env.vendor, 250_000, key)
    assert r1.status_code == r2.status_code == 201
    assert r1.json == r2.json and r2.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in r1.headers
    assert env.balance(env.alice) == a0 - 250_000 and env.balance(env.vendor) == v0 + 250_000
    assert count(env, "SELECT COUNT(*) FROM journal WHERE idem_key=?", key) == 1
    assert count(env, "SELECT COUNT(*) FROM replays WHERE idem_key=? AND same_request=1", key) == 1  # duplicate is visible in data


def test_same_key_racing_ten_times_pays_once(env):
    key, a0 = k(), env.balance(env.alice)
    res = env.race([lambda: env.transfer("alice", env.alice, env.vendor, 100_000, key) for _ in range(10)])
    assert {r.status_code for r in res} == {201}
    assert len({r.json["txn_id"] for r in res}) == 1
    assert sum("Idempotent-Replayed" in r.headers for r in res) == 9
    assert env.balance(env.alice) == a0 - 100_000
    assert count(env, "SELECT COUNT(*) FROM entries WHERE journal_id=(SELECT id FROM journal WHERE idem_key=?)", key) == 2
    assert count(env, "SELECT COUNT(*) FROM journal WHERE idem_key=?", key) == 1
    assert count(env, "SELECT COUNT(*) FROM replays WHERE idem_key=?", key) == 9
    assert env.verify()["ok"]


def test_same_key_different_payload_is_rejected_and_moves_nothing(env):
    key = k()
    env.transfer("alice", env.alice, env.vendor, 1_000, key)
    a1 = env.balance(env.alice)
    r = env.transfer("alice", env.alice, env.vendor, 9_000, key)
    assert r.status_code == 422 and r.json["error"] == "idempotency_key_reuse"
    r = env.transfer("alice", env.alice, env.bob, 1_000, key)
    assert r.status_code == 422
    assert env.balance(env.alice) == a1
    assert env.verify()["stats"]["key_misuse_attempts"] == 2


def test_refusal_is_also_replayed_even_if_funds_arrive_later(env):
    key = k()
    r1 = env.transfer("vendor", env.vendor, env.bob, 5_000, key)          # vendor has 0
    assert r1.status_code == 422 and r1.json["reason"] == "insufficient_funds"
    env.deposit("admin", env.vendor, 50_000, k())
    r2 = env.transfer("vendor", env.vendor, env.bob, 5_000, key)
    assert r2.status_code == 422 and r2.json == r1.json                   # same answer; a new key is needed
    assert env.balance(env.vendor) == 50_000


def test_keys_are_scoped_per_user(env):
    key = k()
    r1 = env.transfer("alice", env.alice, env.vendor, 1_000, key)
    r2 = env.transfer("bob", env.bob, env.vendor, 1_000, key)
    assert r1.status_code == r2.status_code == 201 and r1.json["txn_id"] != r2.json["txn_id"]


def test_missing_or_malformed_key_is_rejected(env):
    a0 = env.balance(env.alice)
    for key in (None, "", "short", "has space in it!!", "x" * 200):
        r = env.transfer("alice", env.alice, env.vendor, 1_000, key)
        assert r.status_code == 400, key
    assert env.balance(env.alice) == a0


def test_crash_at_every_point_leaves_nothing_and_retry_pays_once(env, monkeypatch):
    from app import ledger
    a0, v0 = env.balance(env.alice), env.balance(env.vendor)
    j0 = count(env, "SELECT COUNT(*) FROM journal"); e0 = count(env, "SELECT COUNT(*) FROM entries")
    key = k()
    for point in ("after_journal", "after_entries", "after_balances"):
        def boom(name, point=point):
            if name == point:
                raise RuntimeError("simulated crash at " + point)
        monkeypatch.setattr(ledger, "_fail_point", boom)
        r = env.transfer("alice", env.alice, env.vendor, 70_000, key)
        assert r.status_code == 500
        assert env.balance(env.alice) == a0 and env.balance(env.vendor) == v0
        assert count(env, "SELECT COUNT(*) FROM journal") == j0 and count(env, "SELECT COUNT(*) FROM entries") == e0
    monkeypatch.undo()
    r = env.transfer("alice", env.alice, env.vendor, 70_000, key)      # client retries after the 500s
    assert r.status_code == 201 and "Idempotent-Replayed" not in r.headers
    r = env.transfer("alice", env.alice, env.vendor, 70_000, key)
    assert r.headers["Idempotent-Replayed"] == "true"
    assert env.balance(env.alice) == a0 - 70_000
    assert env.verify()["ok"]
