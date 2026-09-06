"""add idempotency keys table

Stores each client-supplied idempotency key alongside a hash of the request
that claimed it, the transfer it produced, and the exact response that was
returned. The key is claimed inside the same transaction that writes the
transfer, so a committed row always has transfer_id set and there is no
half-finished state to recover from.

The composite primary key (endpoint, idempotency_key) is what makes the whole
scheme work: the unique index is the mutual exclusion. Two concurrent requests
carrying the same key contend on that index at the database, not in any
process's memory, so retries landing on different workers still collapse to
one transfer.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-28T19:22:40.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "idempotency_keys",
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("transfer_id", sa.UUID(), nullable=True),
        sa.Column("response_status", sa.SmallInteger(), nullable=True),
        sa.Column(
            "response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 255",
            name=op.f("ck_idempotency_keys_key_length_sane"),
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name=op.f("ck_idempotency_keys_request_hash_is_sha256"),
        ),
        sa.CheckConstraint(
            "num_nonnulls(transfer_id, response_status, response_body, completed_at) IN (0, 4)",
            name=op.f("ck_idempotency_keys_completion_is_all_or_nothing"),
        ),
        sa.ForeignKeyConstraint(
            ["transfer_id"],
            ["transfers.id"],
            name="fk_idempotency_keys_transfer_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "endpoint", "idempotency_key", name=op.f("pk_idempotency_keys")
        ),
        sa.UniqueConstraint("transfer_id", name=op.f("uq_idempotency_keys_transfer_id")),
    )
    op.create_index(
        "ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_keys_created_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
