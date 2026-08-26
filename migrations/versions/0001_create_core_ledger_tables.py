"""create core ledger tables

Creates the four tables the ledger is built from:

  accounts          who money can belong to (customer wallets and house
                    accounts alike)
  transfers         one business-level money movement
  ledger_entries    the append-only postings that make up a transfer; the
                    source of truth for every balance
  account_balances  a cache of posted totals, reconciled against the entries
                    by scripts/reconcile.py

Correctness that the database can enforce is enforced here rather than in
application code: positive amounts, known account types and directions, a
single supported currency, and composite foreign keys that tie a transfer's
currency to both of its accounts. Every foreign key is ON DELETE RESTRICT --
ledger history is never removed, only compensated by a reversing transfer.

Revision ID: 0001
Revises:
Create Date: 2026-08-25 21:36:11.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('accounts',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('account_type', sa.Text(), nullable=False),
    sa.Column('normal_balance', sa.Text(), sa.Computed("CASE WHEN account_type IN ('asset', 'expense') THEN 'debit'::text ELSE 'credit'::text END", persisted=True), nullable=False),
    sa.Column('currency', sa.String(length=3), server_default=sa.text("'USD'"), nullable=False),
    sa.Column('allow_negative_balance', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("account_type IN ('asset', 'liability', 'equity', 'revenue', 'expense')", name=op.f('ck_accounts_account_type_known')),
    sa.CheckConstraint("currency = 'USD'", name=op.f('ck_accounts_currency_supported')),
    sa.CheckConstraint('length(trim(name)) > 0', name=op.f('ck_accounts_name_not_blank')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_accounts')),
    sa.UniqueConstraint('id', 'currency', name='uq_accounts_id_currency'),
    sa.UniqueConstraint('name', name=op.f('uq_accounts_name'))
    )
    op.create_table('account_balances',
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('posted_debits', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('posted_credits', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('entry_count', sa.BigInteger(), server_default=sa.text('0'), nullable=False),
    sa.Column('last_entry_id', sa.BigInteger(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('entry_count >= 0', name=op.f('ck_account_balances_entry_count_non_negative')),
    sa.CheckConstraint('posted_credits >= 0', name=op.f('ck_account_balances_posted_credits_non_negative')),
    sa.CheckConstraint('posted_debits >= 0', name=op.f('ck_account_balances_posted_debits_non_negative')),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name='fk_account_balances_account_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('account_id', name=op.f('pk_account_balances'))
    )
    op.create_table('transfers',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('source_account_id', sa.UUID(), nullable=False),
    sa.Column('destination_account_id', sa.UUID(), nullable=False),
    sa.Column('amount', sa.BigInteger(), nullable=False),
    sa.Column('currency', sa.String(length=3), server_default=sa.text("'USD'"), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("currency = 'USD'", name=op.f('ck_transfers_currency_supported')),
    sa.CheckConstraint('amount > 0', name=op.f('ck_transfers_amount_positive')),
    sa.CheckConstraint('source_account_id <> destination_account_id', name=op.f('ck_transfers_distinct_accounts')),
    sa.ForeignKeyConstraint(['destination_account_id', 'currency'], ['accounts.id', 'accounts.currency'], name='fk_transfers_destination_account_currency', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['source_account_id', 'currency'], ['accounts.id', 'accounts.currency'], name='fk_transfers_source_account_currency', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_transfers'))
    )
    op.create_index('ix_transfers_created_at', 'transfers', ['created_at'], unique=False)
    op.create_table('ledger_entries',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('transfer_id', sa.UUID(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('direction', sa.Text(), nullable=False),
    sa.Column('amount', sa.BigInteger(), nullable=False),
    sa.Column('signed_amount', sa.BigInteger(), sa.Computed("CASE WHEN direction = 'credit' THEN amount ELSE -amount END", persisted=True), nullable=False),
    sa.Column('currency', sa.String(length=3), server_default=sa.text("'USD'"), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("currency = 'USD'", name=op.f('ck_ledger_entries_currency_supported')),
    sa.CheckConstraint("direction IN ('debit', 'credit')", name=op.f('ck_ledger_entries_direction_known')),
    sa.CheckConstraint('amount > 0', name=op.f('ck_ledger_entries_amount_positive')),
    sa.ForeignKeyConstraint(['account_id', 'currency'], ['accounts.id', 'accounts.currency'], name='fk_ledger_entries_account_currency', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['transfer_id'], ['transfers.id'], name='fk_ledger_entries_transfer_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ledger_entries'))
    )
    op.create_index('ix_ledger_entries_account_id_id', 'ledger_entries', ['account_id', 'id'], unique=False)
    op.create_index('ix_ledger_entries_transfer_id', 'ledger_entries', ['transfer_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_ledger_entries_transfer_id', table_name='ledger_entries')
    op.drop_index('ix_ledger_entries_account_id_id', table_name='ledger_entries')
    op.drop_table('ledger_entries')
    op.drop_index('ix_transfers_created_at', table_name='transfers')
    op.drop_table('transfers')
    op.drop_table('account_balances')
    op.drop_table('accounts')
