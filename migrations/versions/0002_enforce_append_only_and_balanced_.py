"""enforce append only and balanced transfers with triggers

Migration 0001 expressed everything SQLAlchemy can express declaratively. Two
invariants that actually define a ledger cannot be written as CHECK constraints,
because a CHECK sees one row at a time and cannot look at other rows:

  1. The entry log is append-only. UPDATE and DELETE on ledger_entries (and on
     transfers) are rejected outright. A CHECK constraint cannot forbid an
     operation, only constrain the resulting row.

  2. Every transfer balances. SUM(signed_amount) = 0 across all entries sharing
     a transfer_id, and there are at least two of them. This is a multi-row
     condition, and it is only true *between* statements -- after inserting the
     first leg of a transfer the sum is deliberately non-zero. It therefore has
     to be a DEFERRABLE INITIALLY DEFERRED constraint trigger, evaluated at
     COMMIT rather than per statement.

Together these mean an unbalanced transfer cannot be committed by anyone, by
any code path, including psql. Application bugs, a careless migration, and a
hand-typed UPDATE all hit the same wall. That is the point: the ledger's core
invariant does not depend on every future caller remembering to be careful.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-26 19:48:52.000000

"""

from typing import Sequence, Union

from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# --- 1. Append-only enforcement ----------------------------------------------
# One generic function serves both tables; TG_TABLE_NAME and TG_OP report which
# table and which operation was attempted, so the error message is specific
# without needing a function per table.
REJECT_MUTATION_FN = """
CREATE OR REPLACE FUNCTION ledger_reject_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only; % is not permitted', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'restrict_violation',
              HINT = 'Ledger history is never edited. Post a compensating '
                     'reversal transfer instead.';
END;
$$ LANGUAGE plpgsql;
"""

# --- 2. Balanced-transfer enforcement ----------------------------------------
# Runs once per inserted entry, at COMMIT. Re-reading every entry for the
# transfer is cheap because ix_ledger_entries_transfer_id covers exactly this
# lookup, and a transfer has a handful of entries, not thousands.
ASSERT_BALANCED_FN = """
CREATE OR REPLACE FUNCTION ledger_assert_transfer_balances() RETURNS trigger AS $$
DECLARE
    net_signed   bigint;
    entry_count  integer;
BEGIN
    SELECT COALESCE(SUM(signed_amount), 0), COUNT(*)
      INTO net_signed, entry_count
      FROM ledger_entries
     WHERE transfer_id = NEW.transfer_id;

    -- A one-sided posting is money appearing from nowhere. It is the exact
    -- shape of a partially-applied write, so it is worth its own error.
    IF entry_count < 2 THEN
        RAISE EXCEPTION
            'transfer % has only % ledger entry/entries; double-entry requires at least 2',
            NEW.transfer_id, entry_count
            USING ERRCODE = 'check_violation';
    END IF;

    IF net_signed <> 0 THEN
        RAISE EXCEPTION
            'transfer % does not balance: SUM(signed_amount) = %, expected 0',
            NEW.transfer_id, net_signed
            USING ERRCODE = 'check_violation';
    END IF;

    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(REJECT_MUTATION_FN)
    op.execute(ASSERT_BALANCED_FN)

    for table in ("ledger_entries", "transfers"):
        for operation in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER {table}_no_{operation.lower()} "
                f"BEFORE {operation} ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION ledger_reject_mutation();"
            )

    # DEFERRABLE INITIALLY DEFERRED is the whole trick: the check is queued and
    # runs at COMMIT, once both legs of the transfer are present. A normal
    # AFTER INSERT trigger would fire after the first leg and reject every
    # legitimate transfer.
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER ledger_entries_must_balance
        AFTER INSERT ON ledger_entries
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW
        EXECUTE FUNCTION ledger_assert_transfer_balances();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS ledger_entries_must_balance ON ledger_entries;")
    for table in ("ledger_entries", "transfers"):
        for operation in ("update", "delete"):
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_{operation} ON {table};")
    op.execute("DROP FUNCTION IF EXISTS ledger_assert_transfer_balances();")
    op.execute("DROP FUNCTION IF EXISTS ledger_reject_mutation();")
