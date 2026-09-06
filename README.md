# MiniLedger

A small, production-minded double-entry ledger with idempotent money movement,
an independent reconciliation job, and Stripe Issuing card authorisations that
approve or decline against the real ledger balance.

Built to be run, broken, and explained — not to be a feature checklist.

```
FastAPI  ·  PostgreSQL 15  ·  SQLAlchemy 2  ·  Alembic  ·  Stripe Issuing (test mode)  ·  pytest
```

**Four things it is meant to prove**

1. A balance is never a trusted stored number. It is derived from an immutable
   double-entry log, and the database — not the application — enforces that the
   log balances.
2. A retried money movement cannot double-charge, even when the retry arrives
   while the first attempt is still running, on a different worker.
3. A reconciliation job independently re-derives every balance and fails loudly
   on drift, including drift that leaves no arithmetic trace at all.
4. Card authorisations are a real decision point wired to the ledger, not a stub
   that always approves.

---

## Quick start

Needs Docker and Python 3.11+. Commands shown for Windows; on macOS/Linux use
`.venv/bin/` instead of `.venv/Scripts/`.

```bash
git clone <your-fork> && cd miniledger

docker compose up -d                      # Postgres 15 on localhost:5432
cp .env.example .env

python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt

.venv/Scripts/alembic upgrade head        # four migrations
.venv/Scripts/python -m scripts.seed      # chart of accounts + opening balances
.venv/Scripts/python -m pytest            # the whole suite, against real Postgres
```

Run the API:

```bash
.venv/Scripts/python -m uvicorn app.main:app --reload
```

Move some money (account UUIDs come from the seed output or `GET /accounts`):

```bash
curl -s -X POST localhost:8000/transfers \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-001' \
  -d '{"source_account_id":"<alice>","destination_account_id":"<bob>","amount":2500}'
```

Send it again with the same key — same transfer id, `Idempotent-Replay: true`,
and Bob is not paid twice.

Audit the ledger at any point:

```bash
.venv/Scripts/python -m scripts.reconcile     # exit 0 = clean, 1 = drift, 2 = could not run
```

---

## The double-entry model

Money never changes amount, it only changes location.

| Table | What it is |
|---|---|
| `accounts` | Everywhere money can sit. Customer wallets **and** house accounts. |
| `transfers` | One business-level movement. The *intent*. |
| `ledger_entries` | Append-only postings. The *effect*, and the source of truth. |
| `account_balances` | A cache of posted totals. **Not** authoritative. |
| `idempotency_keys` | One claimed key per transfer. |
| `cards`, `card_authorizations` | The Stripe join, and every decision made. |

Every transfer writes at least two entries whose signed amounts sum to exactly
zero. A customer wallet is a **liability** account — the money is theirs, we
merely hold it — which is why customer balances are credit-normal, and why
wallets and house accounts live in one table: it is what makes
`SELECT SUM(signed_amount) FROM ledger_entries` a meaningful whole-system check.

**The direction rule, stated once:** the source account is DEBITED, the
destination is CREDITED. What that does to a balance depends on the account's
normal direction, and exactly one function — `balance_of()` — knows the rule.

### Why balances are derived rather than trusted

`account_balances` exists, and the API reads from it, because summing a log that
grows forever gets slower forever. But it is a **cache with an auditor**, not an
authority:

> The cache is allowed to be wrong. It is not allowed to be wrong *silently*.

Three things make that safe rather than merely hopeful:

- **The application can never invent a balance.** Entries and the cache update
  happen in one SQL statement, where the cache deltas are computed by the
  database *from the rows that statement just inserted*. There is no code path
  that can credit the cache with an amount that is not in the log.
- **Debits and credits are separate running totals**, not one net figure. Both
  only ever increase, so `>= 0` is a real CHECK — and when drift appears, which
  side is wrong narrows the search immediately.
- **Phase 3 re-derives everything** from the log on a schedule and exits
  non-zero on any mismatch.

### What the database enforces, not the application

Two columns are **generated**, so no code path can write them wrongly:
`accounts.normal_balance` (from `account_type`) and `ledger_entries.signed_amount`
(from `direction`). Postgres rejects an `UPDATE` to either.

Two invariants can't be expressed as CHECK constraints — a CHECK sees one row
and cannot forbid an operation — so they are triggers:

- `ledger_entries` and `transfers` **reject UPDATE and DELETE outright.** History
  is corrected by a compensating reversal, never by editing.
- A **`DEFERRABLE INITIALLY DEFERRED`** constraint trigger asserts every transfer
  has ≥2 entries summing to zero. Deferred to `COMMIT`, because after the first
  leg the sum is deliberately non-zero — a plain `AFTER INSERT` trigger would
  reject every legitimate transfer.

Transfers and entries also reference accounts through **composite foreign keys**
on `(id, currency)`, so the database guarantees a transfer's currency matches
both of its accounts.

→ Full table-by-table walkthrough, sign-convention worked example, and an
invariant-by-invariant enforcement table: **[docs/schema.md](docs/schema.md)**

---

## Idempotency

### The failure mode

A client POSTs a $50 transfer. It commits. The response is lost to a timeout.
The client, unable to know it succeeded, retries. **The customer is charged
twice.**

Two details rule out the obvious fixes:

- The retry can arrive **while the first attempt is still running** — so "does
  this transfer already exist?" is answered *no*, correctly, and still wrongly.
- The retry can land on a **different worker process** — so any in-memory cache
  is invisible to it.

The claim has to be made where every request can contend: the database.

### The mechanism

`idempotency_keys` has a composite primary key on `(endpoint, idempotency_key)`.
**That unique index is the mutual exclusion.**

```sql
INSERT INTO idempotency_keys (endpoint, idempotency_key, request_hash)
VALUES (:endpoint, :key, :hash)
ON CONFLICT (endpoint, idempotency_key) DO UPDATE
    SET request_hash = idempotency_keys.request_hash   -- deliberate no-op
RETURNING request_hash, transfer_id, response_status, response_body;
```

**`DO UPDATE`, not `DO NOTHING`.** `DO NOTHING` returns zero rows on conflict —
it tells you a row exists but nothing about it, and takes no lock, so you need a
second `SELECT … FOR UPDATE` with a race between the two statements. `DO UPDATE`
always returns the row *and* holds a lock on it; if a concurrent transaction
holds an uncommitted row for that key, it **blocks** until that resolves. The
block is not a cost — it is the serialisation.

**The `SET` must not use `EXCLUDED.request_hash`.** That would overwrite the
original request's hash with the retry's, destroying the only evidence that two
requests differed, and every conflicting retry would look like a valid replay.

Claim and transfer share **one transaction**, which buys the invariant the whole
design rests on:

> A committed row always has `transfer_id` set.

So `is_new` is a plain NULL check — no status column, no committed
"in progress" state, no background sweeper reaping abandoned claims. A crash
rolls back the claim and the transfer together.

**A failed request releases its key.** The guarantee is *at most one transfer per
key*, not one attempt. Nothing moved, so there is nothing to protect.

### Concurrency

Accounts are locked **one statement at a time, in ascending id order**, so
Alice→Bob and Bob→Alice reach for the same row first and a waits-for cycle
cannot form. It must be separate statements: `ORDER BY … FOR UPDATE` does not
constrain lock *acquisition* order.

Balances are then read in a **second pass**, after every lock is held. This is
the subtle one, and the first implementation got it wrong:

> Under `READ COMMITTED` a statement fixes its snapshot when it **starts**.
> Blocking on a row lock does not advance it. When the lock is granted, Postgres
> re-reads the *locked* row — but a joined table that was not locked is still
> read from the original, stale snapshot.

Ten concurrent requests spending 1000 from a wallet holding 5000 each acquired
the lock in turn and each still saw 5000. All ten were approved. The wallet
ended at **−5000**. The lock was working; the read was from the wrong point in
time. A sequential retry test would never have found it.

`tests/test_concurrent_transfers.py` fires genuinely simultaneous requests at a
real uvicorn server from separate threads, released together on a
`threading.Barrier`.

→ Full write-up including the READ COMMITTED vs SERIALIZABLE trade:
**[docs/idempotency.md](docs/idempotency.md)**

---

## Reconciliation

```bash
python -m scripts.reconcile           # human-readable
python -m scripts.reconcile --json    # for monitoring
```

| Exit | Meaning |
|---|---|
| `0` | Cache agrees with the log, and the log is internally consistent |
| `1` | At least one finding |
| `2` | The job could not run |

**1 and 2 are deliberately different.** "The ledger is broken" and "the auditor
is broken" need different responses. An auditor that could not connect has *not*
given the ledger a clean bill of health.

It is **read-only**. Auto-correcting a balance would destroy the evidence needed
to find the cause, and would paper over an in-flight bug on every run.

### Seeded failure cases

```bash
python -m scripts.break_ledger --scenario tampered-entry
python -m scripts.break_ledger --scenario cache-drift
python -m scripts.break_ledger --scenario double-transfer
```

**`tampered-entry`** — must run `ALTER TABLE ledger_entries DISABLE TRIGGER USER`
before it can write at all. Corrupting the log is not a stray `UPDATE`; it takes
table-owner rights and a deliberate act. Caught **three independent ways**, with
the guilty row named:

```
FAIL - 3 findings

[ledger_does_not_balance] (whole ledger)
  SUM(signed_amount) across all 4 entries is +5000, expected 0

[balance_drift] wallet:alice
  reported balance 500.00 USD does not match the ledger's 550.00 USD
  ...
  entries belonging to transfers that no longer balance (the log itself was altered):
      entry #2  transfer c7001b53-...  credit 55000

[unbalanced_transfer] c7001b53-...
  entries sum to +5000 instead of 0 -- money was created or destroyed
      entry #1  house:float      debit       50000  (signed      -50000)
      entry #2  wallet:alice     credit      55000  (signed      +55000)
```

**`cache-drift`** — no trigger to defeat; `account_balances` is ordinary mutable
state. Exactly one finding, and the report diagnoses the cause rather than
blaming the log: *"no un-folded or unbalanced entries found — the cache was most
likely written to directly."* The customer is shown $550 when only $500 exists.

**`double-transfer`** — the case that justifies the whole design. A duplicate
payment written correctly, bypassing idempotency, leaves a **flawless** ledger:

```
global signed sum        = 0
cache-vs-log mismatches  = 0
```

Every balance check passes. The customer has still been charged twice. It is
caught purely as a **provenance gap** — every transfer written through the
sanctioned path has exactly one `idempotency_keys` row, so one with none was
inserted by something that bypassed it.

**This is why reconciliation checks provenance and not just arithmetic.**

→ **[docs/reconciliation.md](docs/reconciliation.md)**

---

## Stripe Issuing

`POST /webhooks/stripe` handles `issuing_authorization.request` — Stripe's
real-time hook, where it holds a card transaction open and asks whether to let
it through. This endpoint is not acknowledging something that happened; it is
answering while a cardholder stands at a terminal, in about two seconds.

### How the decision ties back to the ledger

Approving posts the debit through **the same `execute_transfer`** the HTTP API
uses — wallet debited, `house:card_settlement` credited — with Stripe's
authorisation id as the idempotency key. Stable across retries, unique per card
transaction: exactly what the Phase 2 machinery wants, so no new mechanism was
needed.

The balance is **not** read before attempting the transfer. That would be the
same time-of-check-to-time-of-use race described above. `execute_transfer` takes
the lock, re-reads, and raises `InsufficientFunds` atomically, so the decline is
*derived from the attempt* rather than guessed before it.

Declines are recorded too, in `card_authorizations`. An approval leaves a
transfer behind; a decline leaves nothing — and "why was my card refused?" is
what card-programme support spends its days on. `balance_at_decision` captures
what the ledger said at that moment, because balances move and "it had enough at
the time" is otherwise unprovable.

### Signature verification

This endpoint is reachable by anyone who learns its URL, and it moves money. The
signature is the only thing between a stranger's `curl` and a $10,000 debit.

`stripe.Webhook.construct_event` recomputes HMAC-SHA256 over
`"<timestamp>.<raw body>"` and compares in constant time, and rejects signatures
outside a 300-second tolerance — without which a captured signature stays valid
forever and anyone who records one real request can replay it indefinitely.

It is given the **raw bytes**. Re-serialising the parsed JSON changes whitespace
and key order, the HMAC stops matching, and every legitimate webhook fails.

Tested with hand-built signatures — including a forged key, a tampered body
carrying a genuine signature, and a stale timestamp — each asserting that **no
money moved**.

### Setup

1. **Stripe test keys.** Create an account, stay in **test mode**, and copy the
   secret key from Developers → API keys. It starts `sk_test_`.

   ```bash
   # .env
   STRIPE_API_KEY=sk_test_...
   ```

   `app/config.py` refuses to boot with anything that is not `sk_test_`. A live
   key cannot reach the webhook handler that approves card spend.

2. **Enable Issuing** — Dashboard → Issuing → Get started, in test mode.

3. **Issue a card:**

   ```bash
   python -m scripts.issue_card --account wallet:alice
   ```

4. **Forward webhooks** ([install the Stripe CLI](https://stripe.com/docs/stripe-cli)):

   ```bash
   stripe listen --forward-to localhost:8000/webhooks/stripe
   ```

   It prints a `whsec_...` signing secret. Put it in `.env` as
   `STRIPE_WEBHOOK_SECRET` and restart the server.

5. **Trigger an authorisation:**

   ```bash
   stripe testhelpers issuing authorizations create --card ic_... --amount 2500
   ```

### Running it without a Stripe account

Issuing must be enabled before test cards exist, which should not stand between
cloning this repo and seeing the flow work. Set any local value for
`STRIPE_WEBHOOK_SECRET`, insert a `cards` row pointing at a wallet, then:

```bash
python -m scripts.simulate_authorization --amount 2500
python -m scripts.simulate_authorization --amount 2500 --authorization-id iauth_a   # redelivery
python -m scripts.simulate_authorization --amount 99999999                          # decline
python -m scripts.simulate_authorization --secret-mismatch                          # forgery
```

It builds a genuine Stripe-format signature that the server verifies with
Stripe's own code, so the decision path and the signature check are both real.
It proves nothing about Stripe actually calling us — for that, use the CLI.

Actual output against a wallet holding 50000:

```
$25 coffee -> expect approve               200  {"approve":true}
same auth redelivered -> no double charge  200  {"approve":true}
$9999 -> expect decline                    200  {"approve":false,"decline_reason":"insufficient_funds"}
forged signature -> expect 400             400  {"error":"SignatureVerificationError", ...}

house:card_settlement      2500
wallet:alice              47500        # charged once, not twice
```

---

## Testing

```bash
.venv/Scripts/python -m pytest        # the suite
.venv/Scripts/ruff check .            # lint
.venv/Scripts/ruff format --check .   # formatting
```

CI runs all three plus the migrations, the seed, and the reconciliation job on a
clean Ubuntu machine with a fresh PostgreSQL 15, on **Python 3.11 and 3.13** —
the repo is developed on 3.13, so the matrix is what makes the "3.11+" claim
true rather than hopeful. It also asserts the *inverse* of the reconciliation
job: each of the three corruption scenarios must make it exit non-zero. A
reconciler that cannot fail is worthless, and only checking the happy path would
never notice.

Everything runs against **real PostgreSQL**, never SQLite and never mocks.
Nearly everything this project claims — deferred constraint triggers, generated
columns, `ON CONFLICT` semantics, row locking, snapshot behaviour — *is*
PostgreSQL behaviour. A stubbed database would prove nothing.

The schema tests deliberately write raw SQL rather than going through `app/`. An
invariant that only holds because the application layer is careful is not an
invariant; it is a convention waiting for the first script somebody writes at
2am.

The session fixture round-trips `downgrade base` → `upgrade head`, so the
downgrade path stays honest rather than being a function nobody runs.

**Four bugs the tests found**, all documented in the commit history:

- Ten concurrent authorisations overdrawing a wallet to −5000 (the snapshot bug).
- Money silently becoming `Decimal` instead of `int`, hidden for two phases
  because `Decimal(5000) == 5000` is `True` — surfaced only when something
  *formatted* a balance instead of comparing it.
- The reconciliation job **hanging forever** on an unreachable database. For a
  scheduled auditor that is the worst failure: it never finishes, never exits
  non-zero, never alerts. Silence looks exactly like success.
- `negative_balance` sitting after the drift early-return, so it could only fire
  on an account already failing another check. An account overdrawn by a bug in
  the posting path updates the cache correctly, produces no drift, and was
  therefore invisible — which is precisely the case it existed to catch. Its
  test had been passing for the wrong reason.

Two tests deserve suspicion rather than trust, and both were found to be
passing for the wrong reason and rewritten: the one above, and a
"503 when unconfigured" test that only passed because `.env` happened to be
empty at the time.

---

## Why SQLAlchemy 2 rather than raw psycopg

The brief left this open. SQLAlchemy, for two reasons and one boundary:

- **Alembic is built on SQLAlchemy metadata.** Raw psycopg means hand-writing
  every migration and losing autogenerate's type-drift detection, which is real
  safety for a schema this constraint-heavy.
- **Session lifecycle and pooling are solved.** Rebuilding them on psycopg would
  be incidental complexity that teaches nothing about ledgers.

The boundary: **the ORM is confined to schema definition and connection
handling.** No lazy loading, no identity map, no `session.add()` in the money
path. Every balance read is an explicit aggregate; every lock is an explicit
`SELECT … FOR UPDATE`; the posting statement and every reconciliation query are
hand-written SQL. Nothing in the money path is ORM magic.

---

## What I deliberately left out

Naming what a ledger *should* have and doesn't is part of the design, not a gap
list. Each of these was considered and scoped out on purpose.

**Multi-currency.** Every table carries `currency`, and transfers and entries
reference accounts through composite FKs on `(id, currency)` so the database
already guarantees they agree. A single `CHECK (currency = 'USD')` pins it, and
dropping that one constraint is the first step of the real work. What is missing
is the hard part: FX rate sourcing, the rounding policy for fractional units,
and separate balances per currency per account. Half-building that would be
worse than pinning it honestly.

**Authorisation and authentication.** There is no login, no API keys, no tenancy.
`POST /transfers` will move money between any two accounts for anyone who can
reach the port. The project is about ledger correctness, and bolting on a token
check would not have made it more correct. Anything internet-facing needs this
before anything else.

**KYC and identity.** The cardholder is created with an obvious placeholder
address. Real programmes need identity verification, sanctions screening,
document collection, and an ongoing monitoring obligation. That is a compliance
product, not a weekend of code, and faking it convincingly would be worse than
not doing it.

**Authorisation holds and the capture lifecycle.** The one simplification I most
want to flag. Approving posts the debit *immediately*. Concurrent
double-spending is **not** a problem — the second authorisation locks the same
row, sees the reduced balance, and declines (there is a test). What is unhandled
is an authorisation that is approved and then never captured, reversed, or
expires: the money has left the wallet and nothing puts it back. Doing it
properly means a transfer whose state changes over time, and transfers are
append-only by design — so the real version is a pending hold plus a
compensating reversal transfer driven by `issuing_authorization.updated` and
`issuing_transaction.created`. That is a coherent chunk of work, and half of it
would be worse than none.

**Reconciliation against Stripe.** The job audits the ledger against itself. A
real card programme also reconciles against the *issuer's* records — Stripe's
authorisations and transactions versus ours — which is where you actually catch
a capture that never arrived. That needs the hold lifecycle above first.

**Production secret management.** Keys come from `.env`. Production wants a
secrets manager, rotation, and a key that never touches a developer's disk. The
one thing that *is* enforced: the app refuses to boot with a non-`sk_test_` key.

**Least-privilege database roles.** Everything runs as the owning role. That is
why `TRUNCATE` can still clear the append-only log — it does not fire row
triggers — and why the corruption script can disable triggers at all. In
production the application role would hold `INSERT`/`SELECT` on `ledger_entries`
and nothing more, making both impossible rather than merely discouraged.

**Horizontal scaling.** One Postgres, one pool, `SELECT … FOR UPDATE`. This is
correct across multiple app workers — the locking is in the database, not the
process — but everything funnels through one primary. Real scale means read
replicas, partitioning the entry log by time, and probably sharding by account,
each of which changes the reconciliation story.

**Idempotency key retention.** Keys accumulate forever. Production ages them out
after ~24h. The index exists; the sweeper does not, because it forces a policy
decision about what a retry arriving after expiry should get, and this project
does not need to answer that.

**Incremental reconciliation.** Every run recomputes the entire log. Fine at this
scale; a real ledger checkpoints and reconciles only since a watermark.
`last_entry_id` exists partly to make that possible later.

**Operational plumbing.** No cron, no alert routing, no dashboards, no tracing.
The reconciliation job has clean exit codes and `--json` precisely so it *can* be
scheduled — wiring it to a specific scheduler is deployment, not ledger design.

---

## Repo layout

```
.github/workflows/   CI: suite + lint + migrations + reconciliation, on 3.11 and 3.13
ruff.toml            lint and format config
app/
  models.py          schema: tables, constraints, generated columns
  ledger.py          balance derivation; the debit/credit sign rule
  transfers.py       posting: ordered locks, overdraft, cache maintenance
  idempotency.py     claiming keys in the database
  reconciliation.py  the independent auditor
  issuing.py         card authorisation decisions
  webhooks.py        Stripe signature verification and dispatch
  main.py            routes, status codes, transaction boundaries
migrations/          four Alembic revisions
scripts/
  seed.py                     chart of accounts + opening balances
  reconcile.py                the audit job
  break_ledger.py             deliberate corruption, three scenarios
  issue_card.py               Stripe cardholder + virtual card
  simulate_authorization.py   signed webhook, no Stripe account needed
tests/               against real Postgres
docs/                schema, idempotency, reconciliation
```
