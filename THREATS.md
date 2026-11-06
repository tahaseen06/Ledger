# THREATS

Format: threat -> what I did -> test that proves it. "Open" items at the bottom are risks I knowingly left, and why.
Every test named here fails if its guard is removed - `python -m scripts.mutation_check` does exactly that, one guard at a time.

## Money
| # | Threat | Defence | Test |
|---|---|---|---|
| M1 | Two withdrawals/transfers race, both see enough balance, both succeed (overdraw) | Check and write happen in ONE `BEGIN IMMEDIATE` transaction. SQLite takes the write lock before the first read, so the second request waits, then sees the reduced balance. No check-then-act gap. | `test_concurrent_withdrawals_cannot_overdraw`, `..._different_payees_cannot_double_spend`, `test_overdraft_limit_is_enforced_under_race` |
| M2 | Lost update: two requests read balance 100, each write 70 | Same lock; balance is written from the value read under the lock, and a DB trigger rejects any balance not equal to the ledger's latest `balance_after`. | M1 tests + `test_database_refuses_edits_and_deletes[UPDATE accounts...]` |
| M3 | Balance driven negative by any route (bug, new endpoint, raw SQL) | App check, plus DB `CHECK (balance >= -overdraft_limit)` (floor is 0 unless an admin granted an overdraft). Two independent layers. | `test_database_refuses_negative_balance...`; mutation "both floors removed" |
| M4 | Money created/lost by a transfer that debits but fails to credit | Debit and credit are rows of the same journal entry inside one transaction; failure at any step rolls everything back. Entries must net to 0. | `test_crash_at_every_point_leaves_nothing...`, `test_every_posted_journal_row_nets_to_zero`, `test_conservation_over_random_sequential_operations` |
| M5 | Crash/exception halfway through a posting | `_post_locked` runs inside try/ROLLBACK; the idempotency record is the journal row itself, written in the same transaction, so a crashed attempt leaves no key and the retry runs cleanly. | `test_crash_at_every_point...` (injects failures after journal / entries / balance writes) |
| M6 | Deadlock between A->B and B->A | One global write lock, always taken first - no lock ordering problem exists. | `test_opposing_transfers_do_not_deadlock_or_lose_money` |
| M7 | Float/rounding errors, huge numbers, negative/zero/NaN amounts, bool/str amounts | Integer paise only; `type(v) is int`, 1 <= v <= 1e9; DB `CHECK(amount>0)`. | `test_invalid_amounts_rejected_without_side_effects` |
| M8 | Transfer to self (to create odd ledger rows) | Rejected before the lock; DB `CHECK(from<>to)`. | `test_self_transfer_unknown_destination_and_external_are_rejected` |
| M9 | Someone moves money to/from the EXTERNAL account directly | Account lookups filter `kind='customer'`; external is unreachable from any user input. | same test |
| M10 | Writes to a frozen account | Frozen check inside the lock; freezing itself also runs under `BEGIN IMMEDIATE`, so a freeze cannot interleave with a posting. | `test_frozen_account_cannot_send_or_receive` |

## Exactly once
| # | Threat | Defence | Test |
|---|---|---|---|
| I1 | Same request replayed after a timeout / double-click | `UNIQUE(user_id, idem_key)` + lookup under the lock; replay returns the stored status+body with `Idempotent-Replayed: true`. | `test_replay_returns_first_result...` |
| I2 | Retry arrives while the first is still in flight | The retry blocks on the write lock, then finds the committed row. 10 racing identical requests -> 1 posting, 9 replays, all same `txn_id`. | `test_same_key_racing_ten_times_pays_once` |
| I3 | Same key reused with a different amount/payee (attacker or client bug) | Request hash stored; mismatch -> 422, nothing moves, logged as misuse. | `test_same_key_different_payload...` |
| I4 | Replay hidden from investigators | Every re-presented key writes an append-only `replays` row (`same_request` 1 = duplicate, 0 = misuse). | asserted in I1/I2/I3 tests; `/api/audit/replays` |
| I5 | One user burns/replays another user's key | Keys are scoped per user. | `test_keys_are_scoped_per_user` |
| I6 | Missing/garbage key makes a request non-idempotent | Key mandatory, validated before anything else. | `test_missing_or_malformed_key_is_rejected` |
| I7 | Refused request retried after funds arrive | Deliberate: the refusal is replayed (same key = same answer). A new key is a new intent. | `test_refusal_is_also_replayed...` |

## Authorisation
| # | Threat | Defence | Test |
|---|---|---|---|
| A1 | Moving another user's money (transfer or withdrawal) | Ownership read **inside** the lock, per request, from the DB; never from the client. Unknown-source and not-yours give identical 403. Denials are logged. | `test_user_cannot_move_another_users_money`, `test_race_between_owner_and_thief_only_owner_succeeds` |
| A2 | Holder mints money via deposit | Deposits are admin-only. | `test_holder_cannot_deposit_or_mint_money` |
| A3 | Auditor writes | Deny-by-default `_authorize` + route role guard. | `test_auditor_is_read_only` |
| A4 | Admin quietly moves customer funds | Admins cannot transfer; they can only deposit/withdraw (visible in the journal with their name) and freeze/adjust limits (logged). | `test_admin_cannot_move_customer_funds...` |
| A5 | Reading others' accounts/statements, IDOR | Non-owner holders get 404 (indistinguishable from nonexistent). | `test_holder_cannot_read_other_accounts...` |
| A6 | Token reuse after logout; brute force | Tokens stored as SHA-256 hashes, 12 h expiry, logout deletes; 5 failed logins / 15 min / username -> 429; scrypt passwords. | `test_logout_invalidates_token`, `test_login_lockout...` |

## Ledger tampering - "what stops me editing a statement?"
1. DB triggers make `journal`, `entries`, `replays`, `security_events` append-only (UPDATE/DELETE abort) and forbid deleting accounts.
2. Triggers force `entries.balance_after` to continue the running balance and `accounts.balance` to equal the latest entry - a balance can't be changed without a ledger entry.
3. **If someone gets past the triggers** (drops them as DB owner): each journal row's SHA-256 covers its fields **and its entries** and the previous row's hash. `GET /api/audit/verify` recomputes the chain, per-journal zero-sum, per-account balance == ledger sum, and global conservation. Tests drop the triggers, rewrite history, and assert detection (`test_tampering_with_triggers_dropped...`, `test_hiding_a_payment...`, `test_balance_changed_directly...`).
4. Refused attempts, denials, replays and admin actions are recorded too.

## Knowingly left OPEN
- **O1 Full-history rewrite / tail truncation by a DB owner is not detectable from inside the database.** The chain is unkeyed SHA-256: someone who can write the file can recompute every hash, or delete the newest rows (still a valid chain). Mitigation not built: periodically copy `head_hash` (shown by verify) somewhere an attacker cannot reach (email, other host, signed log) and compare. `test_deleting_the_newest_journal_rows...` documents this: only an externally held head hash exposes truncation.
- **O2 Single writer, single instance.** SQLite serialises all money moves (fine for this scale; a few hundred/s). Horizontal scaling would need Postgres with `SELECT ... FOR UPDATE` on both accounts in id order (or SERIALIZABLE + retry). I chose correctness by construction over throughput. Multi-process on one host is safe (file locks); network filesystems are not.
- **O3 Lock-wait timeout (15 s) returns 503.** Nothing is committed in that case; retrying with the same key is safe. Under extreme load users see 503s rather than wrong balances.
- **O4 Admin is a trusted role.** No maker-checker (second approver) on deposits/withdrawals; flags (>= INR 50,000, >= 5 moves/10 min) are advisory and do not block or require approval. An admin who is also malicious can mint via deposit - but visibly, under their name.
- **O5 Login lockout can be used to lock a victim out** (DoS) and there is no per-IP rate limiting, MFA, or password reset flow. TLS is the host's job.
- **O6 Destination existence is observable** (404 vs 201) - needed for usable transfers. Account numbers are random 10-digit, not sequential.
- **O7 Idempotency keys are remembered forever** (the journal is permanent), so storage grows and a key can never be reused. Deliberate: expiring keys would reopen double-pay after the window.
- **O8 `verify` is O(n) in memory/time** and runs on request by admin/auditor; a huge ledger would need streaming/paging or a scheduled job.
- **O9 Session tokens live in `sessionStorage`.** Mitigated by strict CSP, no `innerHTML` with user data; an XSS would still steal a token.
- **O10 Test seam:** `ledger._fail_point()` is a no-op hook in production code, used only to inject crashes in tests.

## Not built (and why)
Holds/authorisations, reversals/refunds, scheduled or batch transfers, multi-currency, per-day limits, AI anomaly review. Each adds state machines that multiply the number of ways to lose a rupee; I chose a smaller system I can prove. Reversals would be implemented as new compensating journal entries (never edits). AI, if added, would only write suggestions; a human would still call the same endpoints.
