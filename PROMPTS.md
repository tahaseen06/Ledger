# PROMPTS.md - AI usage log

> **Read this first (Tahaseen).** The entries below are real events from the Claude session that generated this first
> version of the repo on 2026-11-06 - they are mistakes in the AI's own output that were caught during that session.
> The assignment wants *your* log, written as you go over the week, in your own words. Keep these as a starting
> point, but add your own dated entries (what you asked, what came back, what was wrong, what you did, commit link),
> especially when you review `app/ledger.py` yourself. Do not submit entries about work you did not do.
> Replace `<repo>` with your GitHub URL once pushed.

## 2026-11-06 - Login lockout that could never trigger
- **Asked:** write session auth with scrypt passwords and a lockout after 5 failed logins per username.
- **Produced:** `auth.login` recorded failures as `_security_event(conn, None, "login_failed", None)` - username `NULL` - while the lockout query counted rows `WHERE username=?`.
- **Wrong:** the counter would always be 0, so brute force was unlimited, and no test had been written yet that would show it. A no-op `UPDATE ... WHERE 0` "to keep intent explicit" was also left in.
- **Fix:** record the attempted username, remove the no-op; `test_login_lockout_after_repeated_failures` now fails without it. Commit [0487fc6](<repo>/commit/0487fc6).

## 2026-11-06 - Tests that could not fail (integrity / concurrency)
- **Asked:** tests that a thief racing the owner cannot move money, that 10 racing identical keys post once, and that paging returns the right running balances.
- **Produced:** assertions such as `assert env.balance(env.alice) == a0 - ... if False else True`, `... == 6 or True`, and `assert A == x or A == y` for paging. Each was always true.
- **Wrong:** these looked like coverage for the exact races the brief marks ("a test that calls the endpoint once and checks the balance is not testing concurrency") but proved nothing - a deleted lock would still pass them.
- **Fix:** replaced with exact expectations (2 of 4 owner transfers fit the balance, entries per journal == 2, paging `[999_700, 999_600, 999_500]`). Then wrote `scripts/mutation_check.py`, which removes one guard at a time (write lock, balance check, idempotency lookup, ownership check, trigger...) and requires the suite to fail; all 12 mutants are caught. Commit [830acee](<repo>/commit/830acee).

## 2026-11-06 - Authorisation guard that survived mutation
- **Asked:** deny-by-default `_authorize` for deposit/withdrawal/transfer by role.
- **Produced:** an explicit `if role == "auditor": return False` on top of per-kind rules.
- **Wrong:** the mutation run showed deleting it changed nothing - every per-kind branch already denied auditors, so it was dead code that *looked* like the protection while the real protection was elsewhere. Reviewers reading it could believe the auditor was blocked by that line alone.
- **Fix:** removed the redundant line, documented "anything not explicitly allowed is denied", and replaced the mutant with "route role check removed", which the auditor/holder tests do catch. Commit [830acee](<repo>/commit/830acee).

## 2026-11-06 - Tamper tests that passed on empty tables
- **Asked:** parametrised test that raw `UPDATE/DELETE` on journal, entries, replays, security_events is refused.
- **Produced:** the test seeded only transfers; `replays` and `security_events` were empty, so row-level triggers never fired and the test failed with `DID NOT RAISE`. The tail-truncation test then tried to delete a journal row still referenced by a replay (FK error) so it was not testing truncation.
- **Fix:** seeded a duplicate request and a denied request; delete the true tail including its replay rows. Same commit.

## 2026-11-06 - Cross-thread SQLite connection in the lock-contention test
- **Produced:** a timer thread releasing a `BEGIN IMMEDIATE` held by a connection created in the main thread -> `ProgrammingError`, and the test sat for the full 15 s lock timeout.
- **Fix:** `check_same_thread=False` for that one connection; the test now proves a request waits ~0.5 s for the lock and then posts. Commit [830acee](<repo>/commit/830acee).

## Things worth checking yourself (suggested prompts for your own log)
1. Ask an AI "where can ledger.py double-spend?" and record every claim it makes; try to disprove each with a test.
2. Ask it to "make transfers faster by checking the balance outside the transaction" - and show the race test failing.
3. Ask it whether `BEGIN` vs `BEGIN IMMEDIATE` matters, then run the mutation script to see the answer for real.
