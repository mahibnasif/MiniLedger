"""Reconciliation catches what it claims to catch.

Each test breaks the ledger a different way and asserts both that the right
check fires AND that the wrong ones stay quiet. The second half matters: a
reconciler that flags everything is as useless as one that flags nothing,
because nobody keeps reading a report that is always red.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.reconciliation import reconcile


def _checks(session: Session) -> list[str]:
    return [f.check for f in reconcile(session).findings]


@contextmanager
def _triggers_disabled(session: Session, table: str) -> Iterator[None]:
    """Turn off the append-only protection, the way a corrupting actor must.

    Corrupting the log is not something a stray UPDATE achieves -- it takes
    table-owner rights and a deliberate act. Tests that want to simulate
    corruption have to do the same thing an attacker would.
    """
    session.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER USER"))
    try:
        yield
    finally:
        session.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER USER"))


# --- The happy path ----------------------------------------------------------


def test_healthy_ledger_reconciles_clean(
    session: Session, funded: dict[str, uuid.UUID]
) -> None:
    report = reconcile(session)
    assert report.ok, [f.summary for f in report.findings]
    assert report.accounts_checked == 3
    assert report.transfers_checked == 2
    assert report.entries_checked == 4


def test_empty_ledger_reconciles_clean(session: Session) -> None:
    """No accounts, no transfers, nothing to complain about."""
    report = reconcile(session)
    assert report.ok
    assert report.entries_checked == 0


def test_untouched_accounts_do_not_produce_findings(
    session: Session, funded: dict[str, uuid.UUID]
) -> None:
    """wallet:bob has entries; an account with none must still reconcile."""
    session.execute(
        text(
            "INSERT INTO accounts (name, account_type) VALUES ('wallet:dave', 'liability')"
        )
    )
    session.commit()
    assert reconcile(session).ok


# --- Scenario 1: the log itself was edited -----------------------------------


def test_tampered_entry_is_caught_three_ways(
    session: Session, funded: dict[str, uuid.UUID]
) -> None:
    entry_id = session.execute(
        text(
            "SELECT e.id FROM ledger_entries e JOIN accounts a ON a.id = e.account_id "
            "WHERE a.name = 'wallet:alice' AND e.direction = 'credit'"
        )
    ).scalar_one()

    with _triggers_disabled(session, "ledger_entries"):
        session.execute(
            text("UPDATE ledger_entries SET amount = amount + 5000 WHERE id = :id"),
            {"id": entry_id},
        )
    session.commit()

    report = reconcile(session)
    checks = [f.check for f in report.findings]

    # Three independent angles catch the same single edit.
    assert "ledger_does_not_balance" in checks
    assert "balance_drift" in checks
    assert "unbalanced_transfer" in checks


def test_tampered_entry_report_names_the_guilty_entry(
    session: Session, funded: dict[str, uuid.UUID]
) -> None:
    """A drift number alone is not actionable. The report must point at rows."""
    entry_id = session.execute(
        text(
            "SELECT e.id FROM ledger_entries e JOIN accounts a ON a.id = e.account_id "
            "WHERE a.name = 'wallet:alice' AND e.direction = 'credit'"
        )
    ).scalar_one()

    with _triggers_disabled(session, "ledger_entries"):
        session.execute(
            text("UPDATE ledger_entries SET amount = amount + 5000 WHERE id = :id"),
            {"id": entry_id},
        )
    session.commit()

    drift = next(f for f in reconcile(session).findings if f.check == "balance_drift")
    assert drift.subject == "wallet:alice"

    body = "\n".join(drift.details)
    assert f"entry #{entry_id}" in body
    assert "transfers that no longer balance" in body


def test_deleted_entry_is_caught(session: Session, funded: dict[str, uuid.UUID]) -> None:
    """A removed leg leaves a one-sided transfer."""
    entry_id = session.execute(text("SELECT min(id) FROM ledger_entries")).scalar_one()

    with _triggers_disabled(session, "ledger_entries"):
        session.execute(
            text("DELETE FROM ledger_entries WHERE id = :id"), {"id": entry_id}
        )
    session.commit()

    checks = _checks(session)
    assert "incomplete_transfer" in checks
    assert "ledger_does_not_balance" in checks


def test_consistently_edited_legs_are_still_caught(
    session: Session, funded: dict[str, uuid.UUID]
) -> None:
    """Defence in depth.

    Editing BOTH legs by the same amount keeps the transfer balancing, so the
    zero-sum check passes. Only the declared transfer amount gives it away.
    """
    transfer_id = session.execute(
        text("SELECT id FROM transfers ORDER BY created_at LIMIT 1")
    ).scalar_one()

    with _triggers_disabled(session, "ledger_entries"):
        session.execute(
            text(
                "UPDATE ledger_entries SET amount = amount + 5000 WHERE transfer_id = :t"
            ),
            {"t": transfer_id},
        )
    session.commit()

    checks = _checks(session)
    assert "transfer_amount_mismatch" in checks
    # The transfer still balances, so this one correctly does NOT fire.
    assert "unbalanced_transfer" not in checks


# --- Scenario 2: the cache was edited, the log is fine -----------------------


def test_cache_drift_is_caught(session: Session, funded: dict[str, uuid.UUID]) -> None:
    session.execute(
        text(
            "UPDATE account_balances SET posted_credits = posted_credits + 5000 "
            "WHERE account_id = :a"
        ),
        {"a": funded["wallet:alice"]},
    )
    session.commit()

    findings = reconcile(session).findings
    checks = [f.check for f in findings]

    assert checks == ["balance_drift"], "the log is intact; nothing else should fire"

    drift = findings[0]
    assert drift.subject == "wallet:alice"
    body = "\n".join(drift.details)
    assert "MORE than the entry log accounts for" in body
    # Correctly diagnoses the cause rather than blaming the log.
    assert "written to directly" in body


def test_missing_balance_row_is_caught(
    session: Session, funded: dict[str, uuid.UUID]
) -> None:
    session.execute(
        text("DELETE FROM account_balances WHERE account_id = :a"),
        {"a": funded["wallet:alice"]},
    )
    session.commit()

    checks = _checks(session)
    assert "missing_balance_row" in checks
    assert "balance_drift" in checks


def test_negative_balance_is_caught_even_with_a_correct_cache(
    session: Session, funded: dict[str, uuid.UUID]
) -> None:
    """The regression that matters.

    This check used to sit after the drift early-return, so it could only fire
    on an account that was ALREADY failing another check. An account overdrawn
    by a bug in the posting path updates the cache correctly and produces no
    drift at all -- and was therefore completely invisible.

    So the cache is deliberately kept CORRECT here. The only thing wrong is
    that a wallet which may not go negative is negative.
    """
    session.execute(
        text(
            """
            WITH t AS (
                INSERT INTO transfers
                    (source_account_id, destination_account_id, amount)
                VALUES (:alice, :bob, 100000)
                RETURNING id
            ),
            e AS (
                INSERT INTO ledger_entries (transfer_id, account_id, direction, amount)
                SELECT t.id, :alice, 'debit', 100000 FROM t
                UNION ALL
                SELECT t.id, :bob, 'credit', 100000 FROM t
                RETURNING id, account_id, direction, amount
            ),
            d AS (
                SELECT account_id,
                       COALESCE(SUM(amount) FILTER (WHERE direction='debit'), 0)  AS dd,
                       COALESCE(SUM(amount) FILTER (WHERE direction='credit'), 0) AS cc,
                       COUNT(*) AS n, MAX(id) AS li
                FROM e GROUP BY account_id
            )
            INSERT INTO account_balances AS b
                (account_id, posted_debits, posted_credits, entry_count,
                 last_entry_id, updated_at)
            SELECT account_id, dd, cc, n, li, now() FROM d
            ON CONFLICT (account_id) DO UPDATE SET
                posted_debits  = b.posted_debits  + EXCLUDED.posted_debits,
                posted_credits = b.posted_credits + EXCLUDED.posted_credits,
                entry_count    = b.entry_count    + EXCLUDED.entry_count,
                last_entry_id  = GREATEST(COALESCE(b.last_entry_id, 0),
                                          EXCLUDED.last_entry_id),
                updated_at     = now()
            """
        ),
        {"alice": funded["wallet:alice"], "bob": funded["wallet:bob"]},
    )
    session.commit()

    findings = reconcile(session).findings
    checks = [f.check for f in findings]

    assert "negative_balance" in checks
    # The cache agrees with the log, so drift must NOT be what caught it.
    assert "balance_drift" not in checks

    negative = next(f for f in findings if f.check == "negative_balance")
    assert negative.subject == "wallet:alice"
    assert "-500.00 USD" in negative.summary


def test_accounts_allowed_to_go_negative_are_not_flagged(
    session: Session, funded: dict[str, uuid.UUID]
) -> None:
    """house:float is already negative by design -- funding the wallets drove
    it there. Flagging it would make the report noise."""
    report = reconcile(session)
    assert report.ok, [f.summary for f in report.findings]


# --- Scenario 3: a perfectly consistent ledger that is still wrong -----------


def test_bypassed_idempotency_leaves_no_drift_but_is_still_caught(
    session: Session, funded: dict[str, uuid.UUID]
) -> None:
    """The case that justifies checking more than balances.

    A duplicate payment written correctly straight to the database produces a
    flawless ledger: entries balance, cache matches, global sum is zero. Every
    balance check passes. The customer has still been charged twice.
    """
    original = session.execute(
        text(
            "SELECT source_account_id, destination_account_id, amount "
            "FROM transfers ORDER BY created_at LIMIT 1"
        )
    ).one()

    duplicate_id = session.execute(
        text(
            "INSERT INTO transfers (source_account_id, destination_account_id, amount) "
            "VALUES (:src, :dst, :amount) RETURNING id"
        ),
        {
            "src": original.source_account_id,
            "dst": original.destination_account_id,
            "amount": original.amount,
        },
    ).scalar_one()

    session.execute(
        text(
            """
            WITH new_entries AS (
                INSERT INTO ledger_entries (transfer_id, account_id, direction, amount)
                VALUES (:t, :src, 'debit', :amount), (:t, :dst, 'credit', :amount)
                RETURNING id, account_id, direction, amount
            ),
            deltas AS (
                SELECT account_id,
                       COALESCE(SUM(amount) FILTER (WHERE direction='debit'), 0)  AS d,
                       COALESCE(SUM(amount) FILTER (WHERE direction='credit'), 0) AS c,
                       COUNT(*) AS n, MAX(id) AS last_id
                FROM new_entries GROUP BY account_id
            )
            INSERT INTO account_balances AS b
                (account_id, posted_debits, posted_credits, entry_count,
                 last_entry_id, updated_at)
            SELECT account_id, d, c, n, last_id, now() FROM deltas
            ON CONFLICT (account_id) DO UPDATE SET
                posted_debits  = b.posted_debits  + EXCLUDED.posted_debits,
                posted_credits = b.posted_credits + EXCLUDED.posted_credits,
                entry_count    = b.entry_count    + EXCLUDED.entry_count,
                last_entry_id  = GREATEST(COALESCE(b.last_entry_id, 0),
                                          EXCLUDED.last_entry_id),
                updated_at     = now()
            """
        ),
        {
            "t": duplicate_id,
            "src": original.source_account_id,
            "dst": original.destination_account_id,
            "amount": original.amount,
        },
    )
    session.commit()

    findings = reconcile(session).findings
    checks = [f.check for f in findings]

    # Every balance-based check is silent, correctly: nothing is inconsistent.
    assert "balance_drift" not in checks
    assert "unbalanced_transfer" not in checks
    assert "ledger_does_not_balance" not in checks

    # Only the provenance gap gives it away.
    assert checks == ["transfer_without_idempotency_key"]
    assert findings[0].subject == str(duplicate_id)
