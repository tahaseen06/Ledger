import uuid


def k():
    return "k-" + uuid.uuid4().hex


def secev(env, action):
    c = env.raw(); n = c.execute("SELECT COUNT(*) FROM security_events WHERE action=?", (action,)).fetchone()[0]; c.close(); return n


def test_user_cannot_move_another_users_money(env):
    a0, b0 = env.balance(env.alice), env.balance(env.bob)
    before = secev(env, "denied")
    r = env.transfer("bob", env.alice, env.bob, 100_000, k())           # bob tries to pull from alice
    assert r.status_code == 403
    r = env.withdraw("bob", env.alice, 100_000, k())
    assert r.status_code == 403
    r = env.transfer("bob", "LL0000000000", env.bob, 1, k())           # nonexistent source: same answer, no oracle
    assert r.status_code == 403
    assert env.balance(env.alice) == a0 and env.balance(env.bob) == b0
    assert secev(env, "denied") == before + 3                           # every denial is recorded
    assert env.verify()["ok"]


def test_race_between_owner_and_thief_only_owner_succeeds(env):
    a0 = env.balance(env.alice)
    res = env.race([lambda: env.transfer("alice", env.alice, env.vendor, 400_000, k()),
                    lambda: env.transfer("bob", env.alice, env.bob, 400_000, k())] * 4)
    assert [r.status_code for r in res].count(403) == 4                 # every attempt by bob on alice's money
    paid = sum(r.status_code == 201 for r in res)
    assert paid == 2                                                    # alice's own 4 x 400k: only 2 fit in 1,000k
    assert env.balance(env.alice) == a0 - paid * 400_000
    assert env.balance(env.bob) == 500_000                              # bob never gained anything


def test_holder_cannot_deposit_or_mint_money(env):
    a0 = env.balance(env.alice)
    r = env.deposit("alice", env.alice, 1_000_000, k())
    assert r.status_code == 403 and env.balance(env.alice) == a0


def test_auditor_is_read_only(env):
    a0 = env.balance(env.alice)
    for r in (env.transfer("auditor", env.alice, env.bob, 1, k()), env.withdraw("auditor", env.alice, 1, k()),
              env.deposit("auditor", env.alice, 1, k()),
              env.call("auditor", "POST", "/api/accounts", {"owner_username": "alice", "name": "x"}),
              env.call("auditor", "PATCH", f"/api/accounts/{env.alice}", {"frozen": True}),
              env.call("auditor", "POST", "/api/users", {"username": "evil1", "password": "password123", "role": "admin"})):
        assert r.status_code == 403
    assert env.balance(env.alice) == a0
    assert env.call("auditor", "GET", f"/api/accounts/{env.alice}/statement").status_code == 200
    assert env.call("auditor", "GET", "/api/audit/verify").status_code == 200


def test_admin_cannot_move_customer_funds_by_transfer_but_can_deposit_and_withdraw(env):
    assert env.transfer("admin", env.alice, env.bob, 1, k()).status_code == 403
    assert env.deposit("admin", env.alice, 5_000, k()).status_code == 201
    assert env.withdraw("admin", env.alice, 5_000, k()).status_code == 201
    assert env.verify()["ok"]


def test_holder_cannot_read_other_accounts_or_admin_endpoints(env):
    assert env.call("bob", "GET", f"/api/accounts/{env.alice}").status_code == 404
    assert env.call("bob", "GET", f"/api/accounts/{env.alice}/statement").status_code == 404
    assert [a["number"] for a in env.call("bob", "GET", "/api/accounts").json] == [env.bob]
    for path in ("/api/audit/verify", "/api/audit/journal", "/api/audit/replays", "/api/users", "/api/audit/security-events"):
        assert env.call("bob", "GET", path).status_code == 403
    assert env.call("bob", "POST", "/api/accounts", {"owner_username": "bob", "name": "mine"}).status_code == 403
    assert env.call("bob", "PATCH", f"/api/accounts/{env.bob}", {"overdraft_limit_paise": 10**9}).status_code == 403


def test_unauthenticated_and_bad_tokens(env):
    c = env.client()
    assert c.get("/api/accounts").status_code == 401
    assert c.post("/api/transfers", json={}, headers={"Authorization": "Bearer nope", "Idempotency-Key": k()}).status_code == 401


def test_logout_invalidates_token(env):
    c = env.client()
    t = c.post("/api/auth/login", json={"username": "alice", "password": "ChangeMe-Demo-1"}).json["token"]
    h = {"Authorization": "Bearer " + t}
    assert c.get("/api/accounts", headers=h).status_code == 200
    assert c.post("/api/auth/logout", headers=h).status_code == 200
    assert c.get("/api/accounts", headers=h).status_code == 401


def test_login_lockout_after_repeated_failures(env):
    c = env.client()
    for _ in range(5):
        assert c.post("/api/auth/login", json={"username": "bob", "password": "wrong"}).status_code == 401
    assert c.post("/api/auth/login", json={"username": "bob", "password": "ChangeMe-Demo-1"}).status_code == 429


def test_frozen_account_cannot_send_or_receive(env):
    assert env.call("admin", "PATCH", f"/api/accounts/{env.alice}", {"frozen": True}).status_code == 200
    r = env.transfer("alice", env.alice, env.bob, 100, k())
    assert r.status_code == 422 and r.json["reason"] == "account_frozen"
    r = env.transfer("bob", env.bob, env.alice, 100, k())
    assert r.status_code == 422 and r.json["reason"] == "account_frozen"
    assert env.deposit("admin", env.alice, 100, k()).json["reason"] == "account_frozen"
    assert env.verify()["ok"]


def test_admin_cannot_set_overdraft_below_current_position(env):
    env.call("admin", "PATCH", f"/api/accounts/{env.bob}", {"overdraft_limit_paise": 300_000})
    env.transfer("bob", env.bob, env.vendor, 700_000, k())            # bob now at -200,000
    r = env.call("admin", "PATCH", f"/api/accounts/{env.bob}", {"overdraft_limit_paise": 100_000})
    assert r.status_code == 409
