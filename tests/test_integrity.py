"""What stops the ledger being edited, and what happens if someone gets past the database triggers."""
import sqlite3
import uuid

import pytest


def k():
    return "k-" + uuid.uuid4().hex


def seed_activity(env):
    env.transfer("alice", env.alice, env.vendor, 120_000, k())
    env.transfer("vendor", env.vendor, env.bob, 20_000, k())
    env.withdraw("bob", env.bob, 10_000, k())
    env.transfer("vendor", env.vendor, env.bob, 99_999_999, k())      # refused
    dup = k()
    env.transfer("alice", env.alice, env.bob, 5, dup); env.transfer("alice", env.alice, env.bob, 5, dup)   # a replay row
    env.transfer("bob", env.alice, env.bob, 5, k())                                                         # a denial row


@pytest.mark.parametrize("sql", [
    "UPDATE journal SET amount=1 WHERE id=2",
    "DELETE FROM journal WHERE id=2",
    "UPDATE entries SET delta=delta+1 WHERE id=1",
    "DELETE FROM entries WHERE id=1",
    "UPDATE replays SET same_request=0",
    "DELETE FROM security_events",
    "UPDATE accounts SET balance=balance+100 WHERE kind='customer'",          # not backed by an entry
    "UPDATE accounts SET balance=-5 WHERE number='EXTERNAL'",
    "DELETE FROM accounts WHERE kind='customer'",
])
def test_database_refuses_edits_and_deletes(env, sql):
    seed_activity(env)
    c = env.raw()
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(sql)
    c.close()
    assert env.verify()["ok"]


def test_database_refuses_negative_balance_and_unbacked_entries(env):
    c = env.raw()
    with pytest.raises(sqlite3.IntegrityError):                         # CHECK floor
        c.execute("UPDATE accounts SET balance=-1 WHERE number=?", (env.vendor,))
    jid = c.execute("SELECT id FROM journal LIMIT 1").fetchone()[0]
    aid = c.execute("SELECT id FROM accounts WHERE number=?", (env.vendor,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):                         # entry must continue the running balance
        c.execute("INSERT INTO entries(journal_id,account_id,delta,balance_after) VALUES(?,?,?,?)", (jid, aid, 500, 123))
    c.close()


def test_tampering_with_triggers_dropped_is_detected_by_hash_chain(env):
    """Someone with full DB access removes the triggers and rewrites history. verify() must catch it."""
    seed_activity(env)
    assert env.verify()["ok"]
    c = env.raw()
    for t in ("journal_no_update", "entries_no_update", "entries_no_delete", "accounts_balance_matches_ledger"):
        c.execute(f"DROP TRIGGER {t}")
    c.execute("UPDATE journal SET amount=amount-1 WHERE id=(SELECT MIN(id) FROM journal WHERE type='transfer')")
    c.close()
    v = env.verify()
    assert not v["ok"] and any("hash" in p for p in v["problems"])


def test_hiding_a_payment_by_editing_entry_and_balance_is_detected(env):
    seed_activity(env)
    c = env.raw()
    for t in ("entries_no_update", "entries_no_delete", "accounts_balance_matches_ledger", "entries_chain_balance"):
        c.execute(f"DROP TRIGGER {t}")
    eid = c.execute("SELECT id FROM entries WHERE delta<0 AND account_id=(SELECT id FROM accounts WHERE number=?) LIMIT 1",
                    (env.alice,)).fetchone()[0]
    c.execute("DELETE FROM entries WHERE id=?", (eid,))
    c.close()
    assert not env.verify()["ok"]


def test_balance_changed_directly_without_ledger_is_detected(env):
    seed_activity(env)
    c = env.raw()
    c.execute("DROP TRIGGER accounts_balance_matches_ledger")
    c.execute("UPDATE accounts SET balance=balance+777 WHERE number=?", (env.vendor,))
    c.close()
    v = env.verify()
    assert not v["ok"] and any("ledger" in p for p in v["problems"])


def test_deleting_the_newest_journal_rows_is_detected_by_anchored_head(env):
    """Truncating the tail leaves a self-consistent chain; only a head hash kept elsewhere reveals it.
    This documents the known limit: verify() reports head_hash so it can be anchored externally."""
    seed_activity(env)
    head = env.verify()["stats"]["head_hash"]
    c = env.raw()
    for t in ("journal_no_delete", "entries_no_delete", "accounts_balance_matches_ledger"):
        c.execute(f"DROP TRIGGER {t}")
    last = c.execute("SELECT MAX(id) FROM journal").fetchone()[0]
    c.execute("DROP TRIGGER replays_no_delete"); c.execute("DELETE FROM replays WHERE journal_id=?", (last,))
    c.execute("DELETE FROM entries WHERE journal_id=?", (last,))
    c.execute("DELETE FROM journal WHERE id=?", (last,))
    c.close()
    assert env.verify()["stats"]["head_hash"] != head                   # an externally stored head would not match
