# API

Base: `/api`. JSON in/out. `Authorization: Bearer <token>` on everything except login/health.
Money-moving POSTs **require** `Idempotency-Key: <8-128 chars [A-Za-z0-9_.:-]>`. Amounts are `amount_paise` (positive integer, max 1,000,000,000).
Errors: `{"error": code, "message": text}`.

| Endpoint | Who | Effect on money |
|---|---|---|
| `POST /auth/login` `{username,password}` | anyone (5 failures / 15 min / username -> 429) | none; returns token (12 h) |
| `POST /auth/logout` | any signed-in | none; token destroyed |
| `GET /me` | any role | none |
| `POST /users` `{username,password,role}` | admin | none |
| `GET /users` | admin, auditor | none |
| `POST /accounts` `{owner_username,name,overdraft_limit_paise?}` | admin | creates a zero-balance account |
| `PATCH /accounts/{n}` `{frozen?,overdraft_limit_paise?}` | admin | none (cannot touch balances; cannot set a limit that the current balance already violates) |
| `GET /accounts`, `GET /accounts/{n}` | holder: own only (others look like 404); admin, auditor: all | none |
| `GET /accounts/{n}/statement?limit&offset` | same as above | none. Every debit, credit and refused attempt, with running balance and initiator |
| `POST /deposits` `{account,amount_paise}` | **admin only** | EXTERNAL -> account (money enters the system) |
| `POST /withdrawals` `{account,amount_paise}` | admin (any account, as an operator adjustment) or the account's owner | account -> EXTERNAL (money leaves) |
| `POST /transfers` `{from_account,to_account,amount_paise}` | **owner of from_account only** (admin/auditor denied) | from -> to, nets to zero |
| `GET /audit/verify` | admin, auditor | none; recomputes invariants + hash chain |
| `GET /audit/journal?filter=flagged\|refused` | admin, auditor | none |
| `GET /audit/replays` | admin, auditor | none; every duplicate/misused key |
| `GET /audit/security-events` | admin, auditor | none; denials, failed logins, admin actions |
| `GET /health` | anyone | none |

Money endpoints: `201 posted` | `422 refused` (`insufficient_funds`, `account_frozen`; recorded in the ledger) | `403` (recorded in security_events) |
`400` validation | `404` unknown destination | `422 idempotency_key_reuse` | `503` lock wait timed out (nothing happened; retry with the same key).
A replay returns the original status/body plus header `Idempotent-Replayed: true`. A refused request replays as the same refusal - use a new key to try again.
