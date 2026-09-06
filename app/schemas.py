"""Request and response shapes for the HTTP layer."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_account_id: uuid.UUID
    destination_account_id: uuid.UUID

    # Minor units, always. There is no float anywhere in the money path, and
    # accepting "50.00" would introduce one at the boundary.
    amount: int = Field(gt=0, description="Amount in minor units (cents).")

    # Accepted, validated, and then dropped. Stating the single supported
    # currency in the schema turns an unsupported one into a clean 422 at the
    # edge rather than a CHECK violation surfacing from deep in the write path.
    # It is deliberately excluded from the idempotency hash, so a retry that
    # omits the field hashes the same as one that sends "USD" -- they mean the
    # same thing and must not be treated as conflicting requests.
    currency: Literal["USD"] = "USD"

    description: str | None = Field(default=None, max_length=500)


class TransferResponse(BaseModel):
    id: uuid.UUID
    source_account_id: uuid.UUID
    destination_account_id: uuid.UUID
    amount: int
    currency: str
    description: str | None
    created_at: datetime


class AccountResponse(BaseModel):
    """An account and the balance the system currently reports for it.

    `balance` here is the cached figure -- what a customer would be shown. Its
    trustworthiness comes from reconciliation, not from this endpoint.
    """

    id: uuid.UUID
    name: str
    normal_balance: str
    balance: int
    posted_debits: int
    posted_credits: int
    entry_count: int
