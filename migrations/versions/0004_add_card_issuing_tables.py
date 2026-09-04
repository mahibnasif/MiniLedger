"""add card issuing tables

Two tables that join Stripe's world to the ledger.

`cards` maps a Stripe card id to the ledger account whose money it spends. An
authorisation arrives carrying a card id and nothing else, so without this
mapping there is no way to know whose balance to check.

`card_authorizations` records every decision, approved or declined. Declines
are the reason it exists: an approval leaves a transfer behind, a decline
leaves nothing at all, and "why was my card refused?" is the most common
question a card programme has to answer. It is keyed on Stripe's authorisation
id, which doubles as the webhook idempotency guard -- Stripe retries, and a
retry must replay the original decision rather than re-deciding against a
balance that has since moved.

A CHECK ties the decision to its outcome: approved rows must point at the
transfer they created and carry no decline reason; declined rows must carry a
reason and no transfer. An internally contradictory decision record is
unrepresentable rather than merely unlikely.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-03T19:34:52.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('cards',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('stripe_card_id', sa.Text(), nullable=False),
    sa.Column('stripe_cardholder_id', sa.Text(), nullable=False),
    sa.Column('account_id', sa.UUID(), nullable=False),
    sa.Column('last4', sa.String(length=4), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('length(stripe_card_id) > 0', name=op.f('ck_cards_stripe_card_id_not_blank')),
    sa.ForeignKeyConstraint(['account_id'], ['accounts.id'], name='fk_cards_account_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_cards')),
    sa.UniqueConstraint('stripe_card_id', name=op.f('uq_cards_stripe_card_id'))
    )
    op.create_index('ix_cards_account_id', 'cards', ['account_id'], unique=False)
    op.create_table('card_authorizations',
    sa.Column('stripe_authorization_id', sa.Text(), nullable=False),
    sa.Column('card_id', sa.UUID(), nullable=False),
    sa.Column('amount', sa.BigInteger(), nullable=False),
    sa.Column('currency', sa.String(length=3), server_default=sa.text("'USD'"), nullable=False),
    sa.Column('merchant_name', sa.Text(), nullable=True),
    sa.Column('decision', sa.Text(), nullable=False),
    sa.Column('decline_reason', sa.Text(), nullable=True),
    sa.Column('transfer_id', sa.UUID(), nullable=True),
    sa.Column('balance_at_decision', sa.BigInteger(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("(decision = 'approved' AND transfer_id IS NOT NULL                       AND decline_reason IS NULL) OR (decision = 'declined' AND transfer_id IS NULL                       AND decline_reason IS NOT NULL)", name=op.f('ck_card_authorizations_decision_matches_outcome')),
    sa.CheckConstraint("decision IN ('approved', 'declined')", name=op.f('ck_card_authorizations_decision_known')),
    sa.CheckConstraint('amount > 0', name=op.f('ck_card_authorizations_amount_positive')),
    sa.ForeignKeyConstraint(['card_id'], ['cards.id'], name='fk_card_authorizations_card_id', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['transfer_id'], ['transfers.id'], name='fk_card_authorizations_transfer_id', ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('stripe_authorization_id', name=op.f('pk_card_authorizations')),
    sa.UniqueConstraint('transfer_id', name=op.f('uq_card_authorizations_transfer_id'))
    )
    op.create_index('ix_card_authorizations_card_id', 'card_authorizations', ['card_id'], unique=False)
    op.create_index('ix_card_authorizations_created_at', 'card_authorizations', ['created_at'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_card_authorizations_created_at', table_name='card_authorizations')
    op.drop_index('ix_card_authorizations_card_id', table_name='card_authorizations')
    op.drop_table('card_authorizations')
    op.drop_index('ix_cards_account_id', table_name='cards')
    op.drop_table('cards')
