"""MiniLedger schema: a double-entry ledger in four tables.

Vocabulary, because the accounting words matter and are used precisely here:

  posting / entry  One row in `ledger_entries`. A single-sided fact: "account X
                   was debited 500". Entries are append-only and never edited.
  transfer         One row in `transfers`. The business-level event ("move $5
                   from Alice to Bob") that a group of entries belongs to.
  double entry     Every transfer writes >= 2 entries whose signed amounts sum
                   to exactly zero. Money is never created or destroyed, only
                   moved between accounts.

Money is stored as BIGINT in minor units (cents). Never float, never NUMERIC:
floats cannot represent 0.10 exactly, and NUMERIC invites accidental fractional
cents. An integer number of cents is exact, and BIGINT is wide enough that
overflow is not a practical concern.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    MetaData,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# --- Domain vocabulary -------------------------------------------------------
# Kept as Python constants so the application and the CHECK constraints in the
# migrations are written from one source of truth.

ACCOUNT_TYPES = ("asset", "liability", "equity", "revenue", "expense")

# Accounting convention: assets and expenses increase when debited; liabilities,
# equity and revenue increase when credited. A customer wallet is a *liability*
# -- the money is theirs, we merely hold it -- which is why customer balances
# are credit-normal.
DEBIT_NORMAL_TYPES = ("asset", "expense")

DIRECTIONS = ("debit", "credit")

CURRENCY = "USD"


def _sql_tuple(values: tuple[str, ...]) -> str:
    """Render a Python tuple of strings as a SQL IN-list literal."""
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


# Explicit naming convention so Alembic generates stable, greppable constraint
# names. Without it you get anonymous names like "ck_accounts_1", which are
# impossible to reference from a later migration.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Account(Base):
    """Something money can sit in.

    Both customer-facing and internal accounts live here. There is no separate
    "user" table: a wallet is just a liability account, and the house accounts
    that fund it are asset/equity accounts. Keeping them in one table is what
    makes the zero-sum invariant checkable across the whole system.
    """

    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )

    # Unique so seeds and tests can refer to accounts by a stable handle without
    # hardcoding UUIDs.
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    account_type: Mapped[str] = mapped_column(Text, nullable=False)

    # Derived by the database, not by the application. A generated column cannot
    # drift from account_type the way an application-populated column can: there
    # is no code path that sets it, so there is no code path that sets it wrong.
    normal_balance: Mapped[str] = mapped_column(
        Text,
        Computed(
            f"CASE WHEN account_type IN {_sql_tuple(DEBIT_NORMAL_TYPES)} "
            f"THEN 'debit'::text ELSE 'credit'::text END",
            persisted=True,
        ),
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text(f"'{CURRENCY}'")
    )

    # Customer wallets must not go negative; the house accounts that fund them
    # must, or there would be nowhere for opening balances to come from. Making
    # this a per-account flag beats hardcoding "liabilities cannot overdraw",
    # because the rule is then data you can inspect rather than logic you have
    # to go and read.
    allow_negative_balance: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            f"account_type IN {_sql_tuple(ACCOUNT_TYPES)}", name="account_type_known"
        ),
        # Single-currency is a deliberate scope decision, enforced rather than
        # merely documented. When multi-currency arrives this one CHECK is
        # dropped and the composite foreign keys start doing real work.
        CheckConstraint(f"currency = '{CURRENCY}'", name="currency_supported"),
        CheckConstraint("length(trim(name)) > 0", name="name_not_blank"),
        # Not redundant with the primary key: it is the composite target that
        # lets transfers and entries carry a currency the database can verify
        # against the account's own currency.
        UniqueConstraint("id", "currency", name="uq_accounts_id_currency"),
    )


class Transfer(Base):
    """One business-level movement of money, grouping its entries.

    This table records *intent* ("move 500 from A to B"). `ledger_entries`
    records the effect. They are separate because the intent is a single fact
    with a single idempotency key, while the effect is two or more rows that
    must balance.
    """

    __tablename__ = "transfers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )

    source_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    destination_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )

    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text(f"'{CURRENCY}'")
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Zero-amount transfers are almost always a bug (an uninitialised
        # variable), and a negative amount would let a "transfer" run backwards
        # past the overdraft check. Direction is expressed by which account is
        # source and which is destination, never by the sign of the amount.
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            "source_account_id <> destination_account_id", name="distinct_accounts"
        ),
        CheckConstraint(f"currency = '{CURRENCY}'", name="currency_supported"),
        # Composite foreign keys, not plain ones. Referencing (id, currency)
        # rather than just (id) makes the database itself guarantee that a
        # transfer's currency matches both accounts' currency -- an invariant
        # that would otherwise be an application check somebody eventually
        # forgets. Today the CHECK above already pins everything to USD, so
        # this is belt and braces; the day multi-currency lands, dropping that
        # CHECK leaves a schema that is still structurally correct.
        ForeignKeyConstraint(
            ["source_account_id", "currency"],
            ["accounts.id", "accounts.currency"],
            name="fk_transfers_source_account_currency",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["destination_account_id", "currency"],
            ["accounts.id", "accounts.currency"],
            name="fk_transfers_destination_account_currency",
            ondelete="RESTRICT",
        ),
        Index("ix_transfers_created_at", "created_at"),
    )


class LedgerEntry(Base):
    """An immutable, append-only posting. The source of truth for all balances.

    Amounts are always positive and `direction` carries the meaning. This is the
    standard accounting representation and it removes a whole class of sign
    bugs: there is no way to write a "negative credit" that quietly behaves like
    a debit.
    """

    __tablename__ = "ledger_entries"

    # BIGSERIAL rather than a UUID: entries form an ordered log, and a monotonic
    # id gives cheap ordering plus a natural high-water mark for the balance
    # cache to record how far it has folded in.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    transfer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    direction: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # The signed view of the same fact, computed by the database. Its only job
    # is to make the balancing invariant a one-liner:
    #     SUM(signed_amount) = 0, per transfer.
    # Credits are positive by convention. That makes a credit-normal account's
    # balance a plain SUM and a debit-normal account's balance its negation --
    # a conversion that lives in exactly one function (app/ledger.py).
    signed_amount: Mapped[int] = mapped_column(
        BigInteger,
        Computed(
            "CASE WHEN direction = 'credit' THEN amount ELSE -amount END",
            persisted=True,
        ),
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text(f"'{CURRENCY}'")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            f"direction IN {_sql_tuple(DIRECTIONS)}", name="direction_known"
        ),
        CheckConstraint(f"currency = '{CURRENCY}'", name="currency_supported"),
        # RESTRICT, not CASCADE. Deleting a transfer must never silently delete
        # the postings that prove what happened -- if a transfer needs undoing,
        # the answer is a compensating reversal transfer, never a DELETE.
        ForeignKeyConstraint(
            ["transfer_id"],
            ["transfers.id"],
            name="fk_ledger_entries_transfer_id",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["account_id", "currency"],
            ["accounts.id", "accounts.currency"],
            name="fk_ledger_entries_account_currency",
            ondelete="RESTRICT",
        ),
        # Balance derivation reads every entry for one account in id order.
        Index("ix_ledger_entries_account_id_id", "account_id", "id"),
        # Verifying that a transfer balances reads every entry for one transfer.
        Index("ix_ledger_entries_transfer_id", "transfer_id"),
    )


class AccountBalance(Base):
    """A cache of each account's posted totals. NOT the source of truth.

    Summing the whole entry log on every read is correct but gets slower
    forever, so the posted totals are maintained incrementally inside the same
    database transaction that writes the entries.

    That trade is only safe because of Phase 3: reconcile.py independently
    recomputes these totals from `ledger_entries` and fails loudly on any
    mismatch. The cache is allowed to be wrong; it is not allowed to be wrong
    *silently*.

    Debits and credits are tracked separately rather than as one net figure,
    for two reasons:
      1. Both counters only ever increase, so `>= 0` is a real CHECK the
         database can enforce -- a net column could not be constrained at all.
      2. When reconciliation finds drift, knowing which side is wrong narrows
         the search immediately.
    """

    __tablename__ = "account_balances"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "accounts.id", name="fk_account_balances_account_id", ondelete="RESTRICT"
        ),
        primary_key=True,
    )

    posted_debits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    posted_credits: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )

    # Lets reconciliation catch drift that happens to net out to the right
    # number but is built from the wrong set of entries.
    entry_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )

    # Highest ledger_entries.id folded into these totals. Turns "the cache is
    # wrong" into "the cache is wrong and here is where to start looking".
    last_entry_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("posted_debits >= 0", name="posted_debits_non_negative"),
        CheckConstraint("posted_credits >= 0", name="posted_credits_non_negative"),
        CheckConstraint("entry_count >= 0", name="entry_count_non_negative"),
    )


class IdempotencyKey(Base):
    """A client-supplied key claimed in the database, never in memory.

    The whole point is to survive things an in-process cache cannot: a retry
    that lands on a different worker, a retry that arrives while the first
    attempt is still running, and a process that dies mid-transfer.

    The claim and the transfer happen in ONE database transaction. That gives a
    property worth stating explicitly, because the rest of the design leans on
    it:

        A committed row ALWAYS has transfer_id set.

    There is no committed "in progress" state to get stuck in. If a request
    fails or the process dies, the whole transaction rolls back and the key row
    vanishes along with the transfer, so a retry starts cleanly. That is also
    what lets the service distinguish "I just claimed this key" from "somebody
    else already finished it" with a plain NULL check, instead of a status
    column plus a background sweeper to expire abandoned claims.
    """

    __tablename__ = "idempotency_keys"

    # Composite primary key: keys are scoped per endpoint, so reusing the same
    # key string against a different route is not a false cache hit.
    endpoint: Mapped[str] = mapped_column(Text, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(Text, primary_key=True)

    # SHA-256 of the canonical request payload. Retrying a key with different
    # parameters is a client bug, and returning the first call's result would
    # hide it -- so the hash is compared and the mismatch is reported.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    transfer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "transfers.id", name="fk_idempotency_keys_transfer_id", ondelete="RESTRICT"
        ),
        nullable=True,
        # One transfer per key. Two keys pointing at the same transfer would
        # mean idempotency had already failed. NULLs do not collide in a
        # Postgres unique index, which is exactly the behaviour needed here.
        unique=True,
    )

    # The original response, replayed on a retry. Re-serialising from the
    # transfer row would look equivalent but drifts the moment the response
    # shape changes; a replay is supposed to be what the caller saw the first
    # time. jsonb rather than text so an operator can query these directly
    # while debugging. jsonb normalises key order, so this is not returned raw:
    # the route serialises it through the same response_model as the original
    # response, which makes the replay byte-identical rather than merely
    # equivalent.
    response_status: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 255", name="key_length_sane"
        ),
        CheckConstraint("length(request_hash) = 64", name="request_hash_is_sha256"),
        # All four completion columns are set together or not at all. Encodes
        # "a row is either a fresh claim or fully complete, never half-filled"
        # as something the database checks rather than something the service is
        # trusted to do.
        CheckConstraint(
            "num_nonnulls(transfer_id, response_status, response_body, completed_at) "
            "IN (0, 4)",
            name="completion_is_all_or_nothing",
        ),
        # Supports the retention sweep described in the README. Not implemented.
        Index("ix_idempotency_keys_created_at", "created_at"),
    )


class Card(Base):
    """A virtual card issued through Stripe, bound to one ledger account.

    This table is the join between Stripe's world and ours. Stripe knows about
    cards and cardholders; the ledger knows about accounts. An authorization
    arrives carrying a Stripe card id and nothing else, so without this mapping
    there is no way to know whose money is being spent.
    """

    __tablename__ = "cards"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )

    # Unique: one row per Stripe card. Two rows for the same card would make
    # "whose account does this authorisation hit?" ambiguous at exactly the
    # moment it must not be.
    stripe_card_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    stripe_cardholder_id: Mapped[str] = mapped_column(Text, nullable=False)

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", name="fk_cards_account_id", ondelete="RESTRICT"),
        nullable=False,
    )

    # Display only. The full number is never stored, never logged, and never
    # reaches this database -- Stripe holds it, we hold a reference.
    last4: Mapped[str | None] = mapped_column(String(4), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("length(stripe_card_id) > 0", name="stripe_card_id_not_blank"),
        Index("ix_cards_account_id", "account_id"),
    )


class CardAuthorization(Base):
    """Every authorisation decision we made, approved or declined.

    Declines are the reason this table exists. An approval leaves a transfer
    behind; a decline leaves nothing at all, so without a record here there
    would be no way to answer "why was my card refused?" -- which is the single
    most common support question a card programme gets.

    Keyed on Stripe's authorisation id, which also makes webhook delivery
    idempotent: Stripe retries, and a retry finds the decision already recorded
    and replays it rather than deciding again against a balance that has since
    moved.
    """

    __tablename__ = "card_authorizations"

    stripe_authorization_id: Mapped[str] = mapped_column(Text, primary_key=True)

    card_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("cards.id", name="fk_card_authorizations_card_id", ondelete="RESTRICT"),
        nullable=False,
    )

    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text(f"'{CURRENCY}'")
    )
    merchant_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    decision: Mapped[str] = mapped_column(Text, nullable=False)
    decline_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    transfer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "transfers.id",
            name="fk_card_authorizations_transfer_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
        unique=True,
    )

    # What the ledger said at the moment of the decision. Balances move
    # constantly, so "it had enough at the time" is unprovable after the fact
    # unless the figure is captured here. Disputes are won and lost on this.
    balance_at_decision: Mapped[int] = mapped_column(BigInteger, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint(
            "decision IN ('approved', 'declined')", name="decision_known"
        ),
        # An approval must point at the money it moved; a decline must say why
        # and must NOT point at a transfer. Makes an internally contradictory
        # decision record unrepresentable rather than merely unlikely.
        CheckConstraint(
            "(decision = 'approved' AND transfer_id IS NOT NULL "
            "                      AND decline_reason IS NULL) "
            "OR "
            "(decision = 'declined' AND transfer_id IS NULL "
            "                      AND decline_reason IS NOT NULL)",
            name="decision_matches_outcome",
        ),
        Index("ix_card_authorizations_card_id", "card_id"),
        Index("ix_card_authorizations_created_at", "created_at"),
    )
