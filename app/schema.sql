-- LedgerLine schema. All money is INTEGER paise (1 INR = 100 paise). No floats anywhere.
-- Model: double-entry. Every posted movement is >= 2 `entries` rows whose deltas sum to 0.
-- A synthetic EXTERNAL account is the counterparty of deposits/withdrawals, so the
-- sum of ALL account balances (external included) is always exactly 0.

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pw_hash       TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('admin','holder','auditor')),
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash  TEXT PRIMARY KEY,
  user_id     INTEGER NOT NULL REFERENCES users(id),
  expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
  id              INTEGER PRIMARY KEY,
  number          TEXT NOT NULL UNIQUE,
  name            TEXT NOT NULL,
  kind            TEXT NOT NULL CHECK (kind IN ('customer','external')),
  owner_id        INTEGER REFERENCES users(id),
  balance         INTEGER NOT NULL DEFAULT 0,
  overdraft_limit INTEGER NOT NULL DEFAULT 0 CHECK (overdraft_limit >= 0),
  frozen          INTEGER NOT NULL DEFAULT 0 CHECK (frozen IN (0,1)),
  created_at      TEXT NOT NULL,
  -- The floor, enforced by the database itself (second line of defence behind the app check).
  CHECK (kind = 'external' OR balance >= -overdraft_limit),
  CHECK (kind = 'external' OR owner_id IS NOT NULL)
);

-- One row per money-moving ATTEMPT (posted or refused). Append-only and hash-chained.
CREATE TABLE IF NOT EXISTS journal (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  txn_id          TEXT NOT NULL UNIQUE,
  user_id         INTEGER NOT NULL REFERENCES users(id),
  idem_key        TEXT NOT NULL,
  request_hash    TEXT NOT NULL,
  type            TEXT NOT NULL CHECK (type IN ('deposit','withdrawal','transfer')),
  from_account_id INTEGER NOT NULL REFERENCES accounts(id),
  to_account_id   INTEGER NOT NULL REFERENCES accounts(id),
  amount          INTEGER NOT NULL CHECK (amount > 0),
  status          TEXT NOT NULL CHECK (status IN ('posted','refused')),
  reason          TEXT,
  flagged         INTEGER NOT NULL DEFAULT 0,
  flag_reason     TEXT,
  http_status     INTEGER NOT NULL,
  response_json   TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  prev_hash       TEXT NOT NULL,
  row_hash        TEXT NOT NULL,
  -- Idempotency: the key is unique per user, enforced by the database.
  UNIQUE (user_id, idem_key),
  CHECK (from_account_id <> to_account_id)
);

-- Postings. balance_after is the running balance of that account after this entry.
CREATE TABLE IF NOT EXISTS entries (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  journal_id    INTEGER NOT NULL REFERENCES journal(id),
  account_id    INTEGER NOT NULL REFERENCES accounts(id),
  delta         INTEGER NOT NULL CHECK (delta <> 0),
  balance_after INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entries_account ON entries(account_id, id);
CREATE INDEX IF NOT EXISTS idx_entries_journal ON entries(journal_id);
CREATE INDEX IF NOT EXISTS idx_journal_user_time ON journal(user_id, created_at);

-- Every time a key was presented again (same payload = true duplicate; different = misuse).
CREATE TABLE IF NOT EXISTS replays (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  journal_id    INTEGER NOT NULL REFERENCES journal(id),
  user_id       INTEGER NOT NULL,
  idem_key      TEXT NOT NULL,
  same_request  INTEGER NOT NULL,
  created_at    TEXT NOT NULL
);

-- Denied authorisation, failed logins, admin actions (freeze, overdraft change, user/account creation).
CREATE TABLE IF NOT EXISTS security_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER,
  username    TEXT,
  action      TEXT NOT NULL,
  detail      TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_secev_action ON security_events(action, username, created_at);

-- ---------------------------------------------------------------- append-only
CREATE TRIGGER IF NOT EXISTS journal_no_update BEFORE UPDATE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS journal_no_delete BEFORE DELETE ON journal
BEGIN SELECT RAISE(ABORT, 'journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries
BEGIN SELECT RAISE(ABORT, 'entries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries
BEGIN SELECT RAISE(ABORT, 'entries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS replays_no_update BEFORE UPDATE ON replays
BEGIN SELECT RAISE(ABORT, 'replays are append-only'); END;
CREATE TRIGGER IF NOT EXISTS replays_no_delete BEFORE DELETE ON replays
BEGIN SELECT RAISE(ABORT, 'replays are append-only'); END;
CREATE TRIGGER IF NOT EXISTS secev_no_update BEFORE UPDATE ON security_events
BEGIN SELECT RAISE(ABORT, 'security_events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS secev_no_delete BEFORE DELETE ON security_events
BEGIN SELECT RAISE(ABORT, 'security_events are append-only'); END;

-- ------------------------------------------- balance can never diverge from the ledger
-- An entry must continue exactly from the account's previous running balance.
CREATE TRIGGER IF NOT EXISTS entries_chain_balance BEFORE INSERT ON entries
WHEN NEW.balance_after <>
     COALESCE((SELECT balance_after FROM entries WHERE account_id = NEW.account_id
               ORDER BY id DESC LIMIT 1), 0) + NEW.delta
BEGIN SELECT RAISE(ABORT, 'entry does not continue the running balance'); END;

-- accounts.balance may only be set to the latest ledger balance of that account.
CREATE TRIGGER IF NOT EXISTS accounts_balance_matches_ledger BEFORE UPDATE OF balance ON accounts
WHEN NEW.balance <>
     COALESCE((SELECT balance_after FROM entries WHERE account_id = NEW.id
               ORDER BY id DESC LIMIT 1), 0)
BEGIN SELECT RAISE(ABORT, 'balance does not match ledger'); END;

CREATE TRIGGER IF NOT EXISTS accounts_start_at_zero BEFORE INSERT ON accounts
WHEN NEW.balance <> 0
BEGIN SELECT RAISE(ABORT, 'accounts must start at zero'); END;

CREATE TRIGGER IF NOT EXISTS accounts_no_delete BEFORE DELETE ON accounts
BEGIN SELECT RAISE(ABORT, 'accounts cannot be deleted'); END;
