# Reconciliation

Phase 3 deliverable: what the job checks, how to run it, and three seeded
failures it catches.

## Why it exists

Phase 2 made balances fast by maintaining posted totals incrementally in
`account_balances` instead of summing the entry log on every read. That is a
deliberate trade, and it is only honest if something independently checks the
cache against the log.

> The cache is allowed to be wrong. It is not allowed to be wrong *silently*.

Without this job, `account_balances` is a number the application asks you to
trust. With it, it is a number that gets audited on a schedule and fails loudly
the moment it stops being supported by the ledger.

## Running it

```bash
python -m scripts.reconcile           # human-readable report
python -m scripts.reconcile --json    # for monitoring
```

| Exit | Meaning |
|---|---|
| `0` | The cache agrees with the log, and the log is internally consistent |
| `1` | At least one finding — something is wrong |
| `2` | The job could not run (database unreachable, etc.) |

**1 and 2 are deliberately different.** "The ledger is broken" and "the auditor
is broken" demand different responses, and a job that returns the same code for
both trains people to ignore it. An auditor that could not connect has *not*
given the ledger a clean bill of health.

Connection attempts are bounded by `connect_timeout=10`. Without that, an
unreachable host makes the job block forever — it never finishes, never exits
non-zero, and never alerts. A job that silently stops looks exactly like a job
that keeps passing, which is the one failure mode a reconciler must not have.

**It is read-only. It never repairs.** Auto-correcting a balance would destroy
the evidence needed to work out why it drifted, and if the cause were a bug
still in flight it would paper over it on every run. Repair is a human decision,
and it is made by posting a compensating transfer — never by editing history.

## The checks

Two families, because balance comparison alone is not enough.

| Check | Catches |
|---|---|
| `balance_drift` | Cache disagrees with the log for an account |
| `missing_balance_row` | Account has entries but no cache row |
| `negative_balance` | The log puts a restricted account below zero |
| `ledger_does_not_balance` | `SUM(signed_amount)` over all entries ≠ 0 |
| `unbalanced_transfer` | A transfer's entries no longer sum to zero |
| `incomplete_transfer` | A transfer with fewer than two entries |
| `transfer_amount_mismatch` | Declared amount ≠ its debit entries |
| `transfer_without_idempotency_key` | Transfer written outside the idempotent path |
| `card_authorization_mismatch` | Approved authorisation disagrees with the transfer it created |

A drift number alone tells you an account is off by 5000. The log-integrity
checks are what let the report say **which entries did it**.

Two of these catch things no balance comparison ever could.
`transfer_without_idempotency_key` catches a duplicate payment (below).
`card_authorization_mismatch` catches an approved card authorisation whose
transfer disagrees with it — either the amount posted differs from the amount
authorised, or **the debit came out of an account the card does not belong
to**. In that second case every entry balances, the cache matches the log, and
the global signed sum is zero. The ledger is internally flawless and the wrong
customer paid.

### How independent is it, really?

Worth being precise, since "independent" is doing a lot of work in that word.

- The SQL is **written from scratch**, not reused from `app/ledger.py`. A bug in
  the write path's query builder cannot hide by being called again in the
  auditor.
- `balance_of()` **is** shared. It is eight lines of pure arithmetic, unit-tested
  in both debit-normal and credit-normal directions. Duplicating it would mostly
  create an opportunity for the two copies to disagree.
- A genuinely independent auditor would be a **separate service**, ideally in a
  different language, reading a replica. Out of scope here — and named as such
  rather than pretended.

---

## Seeded failure cases

```bash
python -m scripts.break_ledger --scenario tampered-entry
python -m scripts.break_ledger --scenario cache-drift
python -m scripts.break_ledger --scenario double-transfer
```

To reset: `docker compose down -v && docker compose up -d`, then
`alembic upgrade head && python -m scripts.seed`.

### 1. `tampered-entry` — somebody edited the log

The script must run `ALTER TABLE ledger_entries DISABLE TRIGGER USER` before it
can write anything. **That is the point.** Corrupting the log is not a stray
`UPDATE` somebody fat-fingers; it takes table-owner rights and a deliberate act
to switch the protection off first. In production the application role could not
do it at all.

It then inflates Alice's credit entry from 50000 to 55000 — minting $50 that
was never moved into the system.

```
FAIL - 3 findings

[ledger_does_not_balance] (whole ledger)
  SUM(signed_amount) across all 4 entries is +5000, expected 0

[balance_drift] wallet:alice
  reported balance 500.00 USD does not match the ledger's 550.00 USD
  the cache claims 50.00 USD LESS than the entry log accounts for
  debits    reported            0   derived            0
  credits   reported        50000   derived        55000
  entries   reported            1   derived            1
  cache folded up to entry id 2; log high-water mark is 2
  entries belonging to transfers that no longer balance (the log itself was altered):
      entry #2  transfer c7001b53-...  credit 55000

[unbalanced_transfer] c7001b53-...
  entries sum to +5000 instead of 0 -- money was created or destroyed
      entry #1  house:float      debit       50000  (signed      -50000)
      entry #2  wallet:alice     credit      55000  (signed      +55000)
```

**One edit, caught three independent ways**, and the report names the exact row.
Note the direction: the cache reports *less* than the log, because the log is the
thing that was inflated.

### 2. `cache-drift` — somebody ran a bad UPDATE

No trigger to defeat here. `account_balances` is ordinary mutable state with
nothing guarding it, which is precisely why it cannot be trusted unaudited.

```
FAIL - 1 finding

[balance_drift] wallet:alice
  reported balance 550.00 USD does not match the ledger's 500.00 USD
  the cache claims 50.00 USD MORE than the entry log accounts for
  debits    reported            0   derived            0
  credits   reported        55000   derived        50000
  entries   reported            1   derived            1
  cache folded up to entry id 2; log high-water mark is 2
  no un-folded or unbalanced entries found -- the cache was most likely written to directly
```

Exactly one finding — the log is intact, so nothing else fires. The report
correctly diagnoses the *cause*: no un-folded entries and no unbalanced
transfers means the cache itself was written to.

**This is the scariest one in practice.** The customer is shown $550 when only
$500 exists. Everything about the ledger is fine; only the number they see is
wrong.

### 3. `double-transfer` — the case that justifies the whole design

A duplicate payment written correctly, straight to the database, bypassing
idempotency: balanced entries, cache folded in properly.

The resulting ledger is **internally flawless**:

```
global signed sum        = 0
cache-vs-log mismatches  = 0
```

Every balance-based check passes. The customer has still been charged twice.

```
FAIL - 1 finding

[transfer_without_idempotency_key] f28defe3-...
  transfer has no idempotency key, so it was not created through the idempotent API path
      house:float -> wallet:alice  500.00 USD
      created 2026-09-15T09:35:40+00:00
      description: 'opening balance for wallet:alice'
      a duplicate payment written directly to the database looks exactly like this,
      and leaves no balance drift behind to find it by
```

**No amount of balance comparison would ever have found this.** Every transfer
written through `execute_transfer` has exactly one `idempotency_keys` row
pointing at it, enforced by a unique constraint. A transfer with none was
inserted by something that bypassed the idempotent path — which is exactly what
a duplicated payment looks like.

This is why the reconciliation job checks provenance and not just arithmetic.

---

## Testing approach

`tests/test_reconciliation.py` breaks the ledger a different way in each test and
asserts **both** that the right check fires **and that the wrong ones stay
quiet**. The second half matters: a reconciler that flags everything is as
useless as one that flags nothing, because nobody keeps reading a report that is
always red.

Two tests assert the findings list *exactly*:

- cache drift → `["balance_drift"]` and nothing else
- bypassed idempotency → `["transfer_without_idempotency_key"]` and nothing else
- a card authorisation that debited the wrong customer → every balance-based
  check stays silent, so the test fails if `card_authorization_mismatch` ever
  stops being the only thing that catches it

Corruption is simulated the way a real actor would have to do it — disabling the
append-only triggers first — rather than by reaching past the schema in a way
production could not.

`tests/test_reconcile_script.py` pins the exit codes, since that is all a
scheduled job communicates to its monitoring, including that an unreachable
database exits 2 rather than 1, and that the report header never prints the
database password.

## A bug this phase found

Building the report surfaced a latent defect from Phase 1. Postgres widens
`SUM(bigint)` to `NUMERIC`, which psycopg returns as a Python `Decimal`, so every
`derive_*` function had been returning Decimals all along.

It hid because **`Decimal(5000) == 5000` is `True`**. Every arithmetic assertion
in the suite passed and every balance came out right — until `format_minor_units`
ran `:02d` on one and raised. The reconciliation report is the first code to
*format* a derived balance rather than merely compare it.

Fixed by casting aggregates to `BIGINT` in SQL. The regression test asserts the
**types**, because asserting the values would have passed the whole time.

## Not built

- **Scheduling.** No cron, systemd timer or Airflow DAG. The job is designed to
  be scheduled (clean exit codes, `--json`) but wiring it to a specific scheduler
  is deployment, not ledger design.
- **Alerting.** `--json` is the integration point; routing it to PagerDuty is not
  this project's problem.
- **Incremental reconciliation.** Every run recomputes the whole log. That is
  O(all entries) and fine at this scale, but a real ledger would checkpoint and
  reconcile only since the last watermark — `last_entry_id` exists partly to make
  that possible later.
- **Automatic repair.** Deliberate; see above.
