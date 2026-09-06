# Idempotency and concurrency

Phase 2 deliverable: what `POST /transfers` guarantees, how, and what breaks
without each piece.

## The failure mode being prevented

A client POSTs a $50 transfer. The transfer commits. The response is lost to a
timeout. The client, having no way to know it succeeded, retries. Without
idempotency the customer is charged twice.

Two details make this harder than it first looks, and both rule out the obvious
solutions:

1. **The retry can arrive while the first attempt is still running.** So it is
   not enough to check "does this transfer already exist?" — at that moment, it
   doesn't.
2. **The retry can land on a different worker process.** So anything held in
   application memory — a dict, an LRU cache — is invisible to it.

The claim therefore has to be made somewhere every request can contend: the
database.

## The guarantee

> **At most one transfer per idempotency key.**

Note what that does *not* say. It is not "at most one attempt per key". A
request that fails releases its key — see [Failed requests](#failed-requests).

## The mechanism

`idempotency_keys` has a composite primary key on `(endpoint, idempotency_key)`.
**That unique index is the mutual exclusion.** Everything else is bookkeeping
around it.

A transfer runs as one statement sequence in **one transaction**:

```
BEGIN
  1. claim the key          -- INSERT ... ON CONFLICT DO UPDATE
  2. lock both accounts     -- in id order, one statement each
  3. read balances          -- separate statements, fresh snapshot
  4. check overdraft
  5. INSERT transfer + entries + fold into balance cache
  6. record result against the key
COMMIT
```

### Why `DO UPDATE` and not `DO NOTHING`

This was the design decision that mattered most.

`ON CONFLICT DO NOTHING` returns **zero rows** on conflict. That tells you a row
exists but nothing about it, and takes no lock on it. You would then need a
second `SELECT ... FOR UPDATE`, and handle the case where the other transaction
rolled back in between — two statements with a race between them.

`ON CONFLICT DO UPDATE` **always returns the row and always holds a row lock on
it.** If a concurrent transaction holds an uncommitted row for this key,
`DO UPDATE` *blocks* until that transaction resolves, then inserts if it rolled
back or returns the committed row if it did not.

That block is not a cost. It *is* the serialisation: the duplicate waits for the
original to finish, then reads its result.

```sql
INSERT INTO idempotency_keys (endpoint, idempotency_key, request_hash)
VALUES (:endpoint, :key, :hash)
ON CONFLICT (endpoint, idempotency_key) DO UPDATE
    SET request_hash = idempotency_keys.request_hash   -- deliberate no-op
RETURNING request_hash, transfer_id, response_status, response_body;
```

The `SET` writes back the value already there. It exists only to take the lock
and make `RETURNING` produce the existing row.

**It must not be `EXCLUDED.request_hash`.** That would overwrite the *original*
request's hash with the retry's, destroying the only evidence the two differed —
and every conflicting retry would then look like a valid replay. There is a test
for exactly this (`test_claim_does_not_overwrite_the_original_hash`).

### Why one transaction, and what that buys

Because the claim and the postings commit together:

> **A committed row always has `transfer_id` set.**

Which means `is_new` can be a plain `transfer_id IS NULL` check rather than a
status column. A row visible with `transfer_id` still NULL can only be the one
we just inserted ourselves.

The pay-off is that **there is no committed "in progress" state**. A two-phase
design — claim in one transaction, do the work in another — has to handle a
process dying in between, which means a `status` column, an expiry policy, and a
background sweeper to reap abandoned claims. Here, a crash rolls back the claim
and the transfer together and a retry starts clean.

A CHECK constraint enforces that the four completion columns are all set or all
null, so "fresh claim or fully complete, never half-filled" is checked by the
database rather than trusted to the service.

### The request hash

SHA-256 over a canonical JSON rendering (`sort_keys`, fixed separators) of the
semantic fields. Canonical so a retry that serialises its fields in a different
order still hashes identically — otherwise a client changing its JSON library
would start getting spurious conflicts.

Same key + same hash → replay. Same key + different hash → **409**. Returning
the first response there would silently hand the caller a result for a request
they didn't make.

`currency` is deliberately **excluded** from the hash. It is validated at the
edge and then dropped, so omitting it and sending `"USD"` explicitly are the
same request and must not conflict.

### Failed requests

If the transfer raises — insufficient funds, unknown account — the route rolls
back, and the claim row rolls back with it. **The key is reusable.**

This is deliberate. The guarantee sold is *at most one transfer per key*, and no
transfer happened, so there is nothing to protect. Persisting failures would
require a second transaction and reintroduce exactly the stuck-key problem the
single-transaction design avoids.

---

## Concurrency

### Ordered locking

Accounts are locked **one statement at a time, in ascending id order**.

Without ordering: Alice→Bob and Bob→Alice run concurrently, each grabs one
account, each waits for the other. Postgres detects the cycle and kills one
transaction — so no money is corrupted, but a caller who did nothing wrong gets
a 500. An API that randomly returns "deadlock detected" under load is not a
working money API.

With ordering, every transaction reaches for the lowest-id account first, so two
contenders collide on the *same* row first and one simply waits. **A waits-for
cycle cannot form.**

It has to be separate statements, not `WHERE id = ANY(...) ORDER BY id FOR
UPDATE`. `ORDER BY` does not constrain lock acquisition order — the planner may
lock rows as it finds them and sort afterwards.

The test asserts the *order*, not just the resulting balances, because a
balance-only test still passes with the ordering removed and the bug then
surfaces as random deadlocks under load.

### The snapshot bug the tests caught

This one is worth reading twice, because the first implementation was wrong and
only the concurrent test found it.

The obvious implementation joins `account_balances` into the locking `SELECT`
and gets the balance in the same round trip. **It is wrong.**

Under `READ COMMITTED`, a statement fixes its snapshot when it **starts**.
Blocking on a row lock does not advance that snapshot. When the lock is finally
granted, Postgres re-reads the *locked* row to its latest version
(EvalPlanQual) — but a joined table that was not locked is still read from the
original, now-stale snapshot.

Concretely: Alice holds 5000. Ten requests each try to spend 1000.

| | Broken (one statement) | Correct (two passes) |
|---|---|---|
| Waiter acquires lock | yes | yes |
| Balance it sees | 5000 — the pre-race value | current value |
| Requests that succeed | **all 10** | exactly 5 |
| Final balance | **−5000** | 0 |

The lock was working the whole time. The *read* was from the wrong point in
time.

The fix is two passes: acquire every lock in order, **then** read the totals in
separate statements. A new statement takes a new snapshot — and any transaction
that could have moved these balances had to hold these same account locks and
commit before releasing them. So once the locks are held, every such transaction
is committed and visible.

### Why READ COMMITTED and not SERIALIZABLE

`SERIALIZABLE` would also be correct, and would have caught the snapshot bug on
its own by aborting one of the racing transactions.

It was not chosen because it turns every write into a possible
`40001 serialization_failure`, which means a retry loop around every transfer —
and that retry loop has to be idempotency-aware, or retrying internally
re-enters the claim logic. Explicit ordered row locks give exactly the mutual
exclusion needed, with predictable blocking instead of probabilistic aborts, and
no retry machinery.

The trade: this design relies on the locking being right, which is why the lock
ordering and the two-pass read are both directly asserted by tests.

### Pool sizing is part of the design

A duplicate request does not fail fast — it **blocks holding its connection**
until the original commits. So the pool must cover the expected number of
simultaneously blocked retries, or a burst of duplicates starves requests that
would otherwise have succeeded. Set to 20 + 10 overflow.

---

## What the tests actually prove

`tests/test_concurrent_transfers.py` runs a real uvicorn server and fires
requests from separate threads, each with its own connection and session,
released together on a `threading.Barrier`. The barrier matters: without it the
requests trickle in while earlier ones have already committed, and the test
quietly stops testing anything.

| Test | Proves |
|---|---|
| `concurrent_duplicate_requests_create_exactly_one_transfer` | 10 simultaneous duplicates → 1 transfer, 2 entries, 1 key, 9 replays, all same id |
| `concurrent_duplicates_with_a_different_payload_conflict` | Losers get 409, not somebody else's transfer |
| `concurrent_distinct_transfers_do_not_corrupt_the_balance` | No lost updates; cache still matches the log |
| `concurrent_spending_cannot_overdraw` | The check-then-act race; this is the one that failed |
| `opposing_transfers_do_not_deadlock` | Ordered locking; money conserved both ways |

Verified deterministic over repeated runs, not a single lucky pass.

## Response fidelity

A replay is **byte-identical** to the original response. `response_body` is
stored as `jsonb` so an operator can query it in SQL while debugging a duplicate
charge, but jsonb normalises key order — returning it raw would hand back the
same values in a different sequence. Both the original and the replay are
serialised through the route's `response_model`, so they go through one
serialiser and the bytes match. Asserted in `test_api.py`.

## Not built

- **Key retention/expiry.** Keys accumulate forever. Production would age them
  out after ~24h with a sweep on `ix_idempotency_keys_created_at`. Not
  implemented, because doing it properly means deciding what happens to a retry
  that arrives after expiry, which is a policy question this project does not
  need to answer.
- **Persisting failed responses.** See [Failed requests](#failed-requests).
- **Rate limiting / auth.** See the README's "What I deliberately left out".
