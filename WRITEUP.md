# LedgerLine write-up

**Built:** Flask + SQLite service with three roles, accounts, deposits/withdrawals/transfers, per-account statements
(debits, credits, refused attempts, running balance, initiator), idempotency, an append-only hash-chained ledger,
an audit/verify endpoint, rule-based flags, and a small web UI. 55 tests plus a mutation checker.

**Integrity and concurrency model (my words):** every money request is one `BEGIN IMMEDIATE` transaction. SQLite lets
only one writer hold the lock, and IMMEDIATE takes it before any read, so *check balance -> write debit* cannot be
interleaved with another request. Inside that transaction I (1) look up the idempotency key, (2) read ownership, (3) check
frozen/balance, (4) insert the journal row, its entries, and update the balances, then COMMIT. Any failure rolls it all back,
including the key, so a retry after a crash runs fresh; a retry after a success replays the stored answer. The
database re-enforces the rules: CHECK floor, entries must continue the running balance, balance must equal the ledger,
append-only triggers. Ledger model: double-entry with a synthetic EXTERNAL counterparty, so *all balances sum to 0* and
customer balances = deposits - withdrawals. I chose double-entry because zero-sum is a checkable invariant at every level
(row, account, system).

**Idempotency:** key in a header, scoped per user, stored on the journal row forever, with a hash of the request.
Same key + same request = stored response replayed (header says so, `replays` table records it). Same key + different request = 422.
A retry that arrives mid-flight waits on the lock and then replays.

**Deliberately not built:** holds, reversals/refunds, scheduled/batch transfers, multi-currency, maker-checker, AI review,
horizontal scale. See THREATS.md "Not built".

**Unfinished / honest gaps:** not deployed from here (no host credentials; Dockerfile provided); demo video not recorded;
history-rewrite by someone who can write the DB file is only detectable if `head_hash` is anchored externally (O1);
PROMPTS.md contains only the entries from the generating session until you add your own.

**AI use:** see PROMPTS.md - the AI wrote vacuous assertions, a lockout that never fired and a redundant guard; the mutation
checker and review caught them.

## Demo video outline (5 min)
1. Sign in as alice; transfer to vendor; press Submit twice -> one posting (show Replays in the auditor view).
2. Terminal: fire 20 parallel withdrawals (`pytest -k concurrent_withdrawals -s` or a `curl &` loop) -> exactly 3 succeed.
3. Bob tries to move Alice's money -> 403, appears in security events.
4. Auditor: Verify ledger = ok. Then, in a terminal, drop a trigger + edit a journal row -> Verify reports the mismatch.
5. Run `python -m scripts.mutation_check`; explain the three things that cannot be removed.
