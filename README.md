# LedgerLine - money movement with a ledger you can prove

Flask + SQLite (WAL). Double-entry ledger, one write-locked transaction per money move, per-user idempotency keys,
append-only hash-chained journal, three roles. All amounts are **integer paise** (INR). No floats anywhere.

## Run it (clean clone)
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m scripts.seed          # creates ledgerline.db, demo users and accounts (idempotent)
python -m pytest -q             # 55 tests, ~25 s
python -m scripts.mutation_check  # removes one lock/check at a time; every mutant must be caught
flask --app wsgi run            # http://127.0.0.1:5000   (dev)   |   gunicorn -w 1 --threads 8 wsgi:app  (prod)
```
Docker: `docker build -t ledgerline . && docker run -p 8000:8000 -v ll-data:/data ledgerline`.
Any host that runs a container with a **persistent disk** works (Render/Fly/Railway). Use ONE process (`-w 1`) and one
instance: SQLite is a single-writer database and the disk must be local to it.

## Demo accounts (seeded; password `ChangeMe-Demo-1`, override with `LEDGERLINE_DEMO_PASSWORD` before first seed)
| user | role | can do |
|---|---|---|
| `admin` | administrator | create users/accounts, freeze, set overdraft limit, **deposit**, withdraw (adjust), read everything, verify ledger. Cannot transfer customer money. |
| `alice`, `bob`, `vendor` | account holder | see/move **only own** accounts: transfer out, withdraw. Cannot deposit. |
| `auditor` | auditor | read-only: all accounts, statements, journal, replays, security events, verify. Every write is 403. |

Account numbers are printed by `python -m scripts.seed` and shown in the UI after sign-in. **Change the demo passwords on any public deployment you care about.**

## Layout
`app/ledger.py` money engine - `app/schema.sql` tables + triggers - `app/routes.py` API - `app/auth.py` - `tests/` - `scripts/`
Docs: `API.md`, `THREATS.md`, `PROMPTS.md`, `WRITEUP.md`.

## The invariant (state it, check it)
1. Every posted movement is >= 2 `entries` whose deltas sum to **0**. A synthetic `EXTERNAL` account is the counterparty of deposits/withdrawals, so the sum of **all** balances is always 0.
2. Sum of customer balances = total deposits - total withdrawals. Transfers cannot change it.
3. For each account: `balance == last entry.balance_after == SUM(entries.delta)` and `balance >= -overdraft_limit` (default floor 0).
`GET /api/audit/verify` recomputes all of this plus the hash chain from scratch.
