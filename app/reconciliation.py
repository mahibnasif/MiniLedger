"""Reconciliation: the independent auditor of the balance cache.

Phase 2 made `account_balances` fast by maintaining posted totals incrementally
instead of summing the log on every read. That trade is only honest if
something independently checks the cache against the log and refuses to let a
mismatch reach a customer. This is that something.

> The cache is allowed to be wrong. It is not allowed to be wrong *silently*.

HOW INDEPENDENT IS THIS, REALLY?

Worth being precise, because "independent" is doing a lot of work in that
sentence.

  * The SQL here is written from scratch rather than calling
    `derive_all_snapshots` from app/ledger.py. A bug in the write path's query
    builder therefore cannot hide itself by being reused here.
  * `balance_of` IS shared. It is eight lines of pure arithmetic with no
    database access, unit-tested in both debit-normal and credit-normal
    directions. Duplicating it would mostly create an opportunity for the two
    copies to disagree.
  * A genuinely independent auditor would be a separate service, ideally in a
    different language, reading a replica. That is out of scope here and is
    named as such in the README.

TWO FAMILIES OF CHECK, AND WHY BOTH ARE NEEDED

  Balance drift  Does the cache agree with the log? Catches a bad UPDATE, a
                 half-applied write, a cache that missed entries.

  Log integrity  Is the log itself internally consistent? Catches tampering.
                 If somebody edits an entry's amount, the cache and the log
                 disagree -- but so does that transfer with itself, and the
                 second fact names the exact rows responsible.

A drift number alone tells you an account is off by 5000. The log-integrity
checks are what let the report say *which entries* did it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ledger import AccountTotals, balance_of, format_minor_units


@dataclass(frozen=True)
class Finding:
    """One thing that is wrong. `check` is a stable slug for alerting."""

    check: str
    subject: str
    summary: str
    details: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "check": self.check,
            "subject": self.subject,
            "summary": self.summary,
            "details": list(self.details),
        }


@dataclass
class ReconciliationReport:
    accounts_checked: int = 0
    transfers_checked: int = 0
    entries_checked: int = 0
    card_authorizations_checked: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "accounts_checked": self.accounts_checked,
            "transfers_checked": self.transfers_checked,
            "entries_checked": self.entries_checked,
            "card_authorizations_checked": self.card_authorizations_checked,
            "findings": [f.as_dict() for f in self.findings],
        }


# --- Check 1: does the cache agree with the log? -----------------------------

# Independently written: recomputes every account's totals straight from the
# entry log and puts them next to whatever account_balances currently claims.
# LEFT JOINs both ways so an account with no entries, or no cache row, still
# appears rather than quietly dropping out of the audit.
_DERIVED_VS_REPORTED = text(
    """
    WITH derived AS (
        SELECT a.id AS account_id,
               COALESCE(SUM(e.amount) FILTER (WHERE e.direction = 'debit'), 0)::bigint  AS debits,
               COALESCE(SUM(e.amount) FILTER (WHERE e.direction = 'credit'), 0)::bigint AS credits,
               COUNT(e.id) AS entry_count,
               MAX(e.id)   AS last_entry_id
        FROM accounts a
        LEFT JOIN ledger_entries e ON e.account_id = a.id
        GROUP BY a.id
    )
    SELECT a.name,
           a.id                              AS account_id,
           a.normal_balance,
           a.allow_negative_balance,
           d.debits                          AS derived_debits,
           d.credits                         AS derived_credits,
           d.entry_count                     AS derived_entry_count,
           d.last_entry_id                   AS derived_last_entry_id,
           COALESCE(b.posted_debits, 0)      AS reported_debits,
           COALESCE(b.posted_credits, 0)     AS reported_credits,
           COALESCE(b.entry_count, 0)        AS reported_entry_count,
           b.last_entry_id                   AS reported_last_entry_id,
           (b.account_id IS NULL)            AS missing_cache_row
    FROM accounts a
    JOIN derived d ON d.account_id = a.id
    LEFT JOIN account_balances b ON b.account_id = a.id
    ORDER BY a.name
    """
)

# Entries an account has that the cache never folded in. When the cache simply
# fell behind, this names the exact rows it is missing.
_ENTRIES_NOT_FOLDED_IN = text(
    """
    SELECT id, transfer_id, direction, amount
    FROM ledger_entries
    WHERE account_id = :account_id
      AND id > COALESCE(:last_entry_id, 0)
    ORDER BY id
    LIMIT 20
    """
)

# Entries of this account that belong to a transfer which does not balance.
# When the cache is up to date but the LOG was edited, these are the culprits.
_SUSPECT_ENTRIES = text(
    """
    SELECT e.id, e.transfer_id, e.direction, e.amount
    FROM ledger_entries e
    WHERE e.account_id = :account_id
      AND e.transfer_id IN (
          SELECT transfer_id FROM ledger_entries
          GROUP BY transfer_id
          HAVING COALESCE(SUM(signed_amount), 0) <> 0
      )
    ORDER BY e.id
    LIMIT 20
    """
)


def _check_balance_drift(session: Session, report: ReconciliationReport) -> None:
    rows = session.execute(_DERIVED_VS_REPORTED).all()
    report.accounts_checked = len(rows)

    for row in rows:
        derived = AccountTotals(
            posted_debits=row.derived_debits,
            posted_credits=row.derived_credits,
            entry_count=row.derived_entry_count,
            last_entry_id=row.derived_last_entry_id,
        )
        reported = AccountTotals(
            posted_debits=row.reported_debits,
            posted_credits=row.reported_credits,
            entry_count=row.reported_entry_count,
            last_entry_id=row.reported_last_entry_id,
        )

        if row.missing_cache_row and derived.entry_count > 0:
            report.findings.append(
                Finding(
                    check="missing_balance_row",
                    subject=row.name,
                    summary=(
                        f"account has {derived.entry_count} ledger entries but no "
                        f"account_balances row, so it reports a balance of 0"
                    ),
                )
            )

        derived_balance = balance_of(row.normal_balance, derived)
        reported_balance = balance_of(row.normal_balance, reported)

        # Checked for EVERY account, before the drift early-return below.
        #
        # This started life inside the drift block, which meant it could only
        # ever fire on an account that was already failing another check. An
        # account overdrawn by a bug in the posting path -- which updates the
        # cache correctly, so there is no drift -- was invisible. That is
        # exactly the case worth catching: drift means the cache is lying,
        # whereas this means the LEDGER ITSELF records money that should never
        # have been allowed to leave.
        if derived_balance < 0 and not row.allow_negative_balance:
            report.findings.append(
                Finding(
                    check="negative_balance",
                    subject=row.name,
                    summary=(
                        f"account is not permitted to go negative but the ledger "
                        f"puts it at {format_minor_units(derived_balance)}"
                    ),
                    details=(
                        f"derived from {derived.entry_count} entries: "
                        f"{derived.posted_debits} debited, "
                        f"{derived.posted_credits} credited",
                        "no code path should be able to produce this -- treat it "
                        "as a bug in the overdraft check, not as bad data",
                    ),
                )
            )

        if derived == reported:
            continue

        delta = reported_balance - derived_balance

        direction = "MORE" if delta > 0 else "LESS"
        details = [
            f"the cache claims {format_minor_units(abs(delta))} {direction} "
            f"than the entry log accounts for",
            f"debits    reported {reported.posted_debits:>12}   "
            f"derived {derived.posted_debits:>12}",
            f"credits   reported {reported.posted_credits:>12}   "
            f"derived {derived.posted_credits:>12}",
            f"entries   reported {reported.entry_count:>12}   "
            f"derived {derived.entry_count:>12}",
            f"cache folded up to entry id {reported.last_entry_id}; "
            f"log high-water mark is {derived.last_entry_id}",
        ]

        # Explain WHICH entries are responsible. Two different causes, two
        # different explanations -- which one applies is worth reporting,
        # because they call for different remediation.
        not_folded = session.execute(
            _ENTRIES_NOT_FOLDED_IN,
            {"account_id": row.account_id, "last_entry_id": reported.last_entry_id},
        ).all()
        if not_folded:
            details.append("entries the cache never folded in:")
            details += [
                f"    entry #{e.id}  transfer {e.transfer_id}  "
                f"{e.direction:<6} {e.amount}"
                for e in not_folded
            ]

        suspects = session.execute(_SUSPECT_ENTRIES, {"account_id": row.account_id}).all()
        if suspects:
            details.append(
                "entries belonging to transfers that no longer balance "
                "(the log itself was altered):"
            )
            details += [
                f"    entry #{e.id}  transfer {e.transfer_id}  "
                f"{e.direction:<6} {e.amount}"
                for e in suspects
            ]

        if not not_folded and not suspects:
            details.append(
                "no un-folded or unbalanced entries found -- the cache was "
                "most likely written to directly"
            )

        report.findings.append(
            Finding(
                check="balance_drift",
                subject=row.name,
                summary=(
                    f"reported balance {format_minor_units(reported_balance)} "
                    f"does not match the ledger's "
                    f"{format_minor_units(derived_balance)}"
                ),
                details=tuple(details),
            )
        )


# --- Check 2: is the log internally consistent? ------------------------------

_TRANSFER_INTEGRITY = text(
    """
    SELECT t.id,
           t.amount                                                    AS declared_amount,
           COUNT(e.id)                                                 AS entry_count,
           COALESCE(SUM(e.signed_amount), 0)::bigint                   AS net_signed,
           COALESCE(SUM(e.amount) FILTER (WHERE e.direction='debit'), 0)::bigint AS debit_total
    FROM transfers t
    LEFT JOIN ledger_entries e ON e.transfer_id = t.id
    GROUP BY t.id, t.amount
    ORDER BY t.created_at
    """
)

_ENTRIES_FOR_TRANSFER = text(
    """
    SELECT e.id, a.name AS account_name, e.direction, e.amount, e.signed_amount
    FROM ledger_entries e
    JOIN accounts a ON a.id = e.account_id
    WHERE e.transfer_id = :transfer_id
    ORDER BY e.id
    """
)


def _entry_lines(session: Session, transfer_id: uuid.UUID) -> list[str]:
    rows = session.execute(_ENTRIES_FOR_TRANSFER, {"transfer_id": transfer_id}).all()
    return [
        f"    entry #{r.id}  {r.account_name:<16} {r.direction:<6} "
        f"{r.amount:>10}  (signed {r.signed_amount:>+11})"
        for r in rows
    ]


def _check_transfer_integrity(session: Session, report: ReconciliationReport) -> None:
    rows = session.execute(_TRANSFER_INTEGRITY).all()
    report.transfers_checked = len(rows)

    for row in rows:
        if row.entry_count < 2:
            report.findings.append(
                Finding(
                    check="incomplete_transfer",
                    subject=str(row.id),
                    summary=(
                        f"transfer has {row.entry_count} ledger entries; "
                        f"double-entry requires at least 2"
                    ),
                    details=tuple(_entry_lines(session, row.id)),
                )
            )
            continue

        if row.net_signed != 0:
            report.findings.append(
                Finding(
                    check="unbalanced_transfer",
                    subject=str(row.id),
                    summary=(
                        f"entries sum to {row.net_signed:+d} instead of 0 -- "
                        f"money was created or destroyed"
                    ),
                    details=tuple(_entry_lines(session, row.id)),
                )
            )

        # Defence in depth: if BOTH legs were edited by the same amount the
        # transfer still balances, and only the declared amount gives it away.
        if row.declared_amount != row.debit_total:
            report.findings.append(
                Finding(
                    check="transfer_amount_mismatch",
                    subject=str(row.id),
                    summary=(
                        f"transfer declares {row.declared_amount} but its debit "
                        f"entries total {row.debit_total}"
                    ),
                    details=tuple(_entry_lines(session, row.id)),
                )
            )


def _check_ledger_totals(session: Session, report: ReconciliationReport) -> None:
    """The one-line health check for the whole system.

    Balances are signed per account direction and do not sum to zero. The raw
    signed amounts do, across every entry ever written. If this is non-zero,
    money exists in the ledger that was never moved into it.
    """
    row = session.execute(
        text(
            "SELECT COUNT(*) AS n, COALESCE(SUM(signed_amount), 0)::bigint AS net "
            "FROM ledger_entries"
        )
    ).one()
    report.entries_checked = row.n

    if row.net != 0:
        report.findings.append(
            Finding(
                check="ledger_does_not_balance",
                subject="(whole ledger)",
                summary=(
                    f"SUM(signed_amount) across all {row.n} entries is "
                    f"{row.net:+d}, expected 0"
                ),
            )
        )


# --- Check 3: was every transfer created through the sanctioned path? --------

# Every transfer written through execute_transfer has exactly one
# idempotency_keys row pointing at it (enforced unique). A transfer with none
# was inserted by something that bypassed the idempotent path -- which is
# exactly what a duplicated payment looks like.
#
# This check exists because a bypassed-idempotency double transfer produces a
# ledger that is PERFECTLY consistent: balanced entries, a correct cache, no
# drift at all. No amount of balance comparison will ever find it. It is only
# visible as a provenance gap.
_TRANSFERS_WITHOUT_KEYS = text(
    """
    SELECT t.id, t.amount, t.description, t.created_at,
           src.name AS source_name, dst.name AS destination_name
    FROM transfers t
    JOIN accounts src ON src.id = t.source_account_id
    JOIN accounts dst ON dst.id = t.destination_account_id
    LEFT JOIN idempotency_keys k ON k.transfer_id = t.id
    WHERE k.transfer_id IS NULL
    ORDER BY t.created_at
    """
)


def _check_transfer_provenance(session: Session, report: ReconciliationReport) -> None:
    for row in session.execute(_TRANSFERS_WITHOUT_KEYS).all():
        report.findings.append(
            Finding(
                check="transfer_without_idempotency_key",
                subject=str(row.id),
                summary=(
                    "transfer has no idempotency key, so it was not created "
                    "through the idempotent API path"
                ),
                details=(
                    f"    {row.source_name} -> {row.destination_name}  "
                    f"{format_minor_units(row.amount)}",
                    f"    created {row.created_at.isoformat()}",
                    f"    description: {row.description!r}",
                    "    a duplicate payment written directly to the database "
                    "looks exactly like this, and leaves no balance drift "
                    "behind to find it by",
                ),
            )
        )


# --- Check 4: does each card decision match the money it claims to have moved?

# The foreign key guarantees an approved authorisation POINTS AT a transfer. It
# says nothing about whether that transfer is the right one. Two ways a card
# programme gets this wrong, both silent without this check:
#
#   * the amount posted differs from the amount authorised -- the customer is
#     charged something other than what the terminal showed them;
#   * the debit came out of a different account than the card belongs to --
#     the wrong customer paid, and every balance still reconciles perfectly
#     because the ledger itself is internally consistent.
#
# The second one is the reason this exists. Nothing else in this file would
# notice it.
_CARD_AUTHORIZATION_MISMATCH = text(
    """
    SELECT ca.stripe_authorization_id,
           ca.amount                AS authorised_amount,
           t.amount                 AS transfer_amount,
           card_account.name        AS card_account_name,
           source_account.name      AS debited_account_name,
           (ca.amount <> t.amount)                      AS amount_differs,
           (t.source_account_id <> c.account_id)        AS wrong_account
    FROM card_authorizations ca
    JOIN cards c            ON c.id = ca.card_id
    JOIN transfers t        ON t.id = ca.transfer_id
    JOIN accounts card_account   ON card_account.id = c.account_id
    JOIN accounts source_account ON source_account.id = t.source_account_id
    WHERE ca.decision = 'approved'
      AND (ca.amount <> t.amount OR t.source_account_id <> c.account_id)
    ORDER BY ca.created_at
    """
)


def _check_card_authorizations(session: Session, report: ReconciliationReport) -> None:
    report.card_authorizations_checked = session.execute(
        text("SELECT count(*) FROM card_authorizations")
    ).scalar_one()

    for row in session.execute(_CARD_AUTHORIZATION_MISMATCH).all():
        problems = []
        if row.amount_differs:
            problems.append(
                f"authorised {format_minor_units(row.authorised_amount)} but the "
                f"transfer moved {format_minor_units(row.transfer_amount)}"
            )
        if row.wrong_account:
            problems.append(
                f"card belongs to {row.card_account_name} but "
                f"{row.debited_account_name} was debited"
            )

        report.findings.append(
            Finding(
                check="card_authorization_mismatch",
                subject=row.stripe_authorization_id,
                summary="; ".join(problems),
                details=(
                    "the ledger is internally consistent here -- no balance "
                    "check would catch this",
                ),
            )
        )


def reconcile(session: Session) -> ReconciliationReport:
    """Run every check and return what was found. Never raises on bad data."""
    report = ReconciliationReport()
    _check_ledger_totals(session, report)
    _check_balance_drift(session, report)
    _check_transfer_integrity(session, report)
    _check_transfer_provenance(session, report)
    _check_card_authorizations(session, report)
    return report
