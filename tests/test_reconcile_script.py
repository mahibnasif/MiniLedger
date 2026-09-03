"""The reconcile CLI. Exit codes are the operational contract.

This job is meant to run unattended on a schedule, so its exit code is the only
thing most consumers will ever look at. Getting it wrong means either silent
corruption or a permanently red alert nobody trusts.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from scripts import reconcile as script


@pytest.fixture()
def cli(session: Session, monkeypatch: pytest.MonkeyPatch):
    """Point the script's session_scope at the test database."""

    @contextmanager
    def fake_scope() -> Iterator[Session]:
        yield session

    monkeypatch.setattr(script, "session_scope", fake_scope)
    return script


def test_clean_ledger_exits_zero(
    cli, funded: dict[str, uuid.UUID], capsys: pytest.CaptureFixture
) -> None:
    assert cli.main([]) == script.EXIT_OK
    assert "PASS" in capsys.readouterr().out


def test_drift_exits_one(
    cli, session: Session, funded: dict[str, uuid.UUID], capsys: pytest.CaptureFixture
) -> None:
    session.execute(
        text(
            "UPDATE account_balances SET posted_credits = posted_credits + 5000 "
            "WHERE account_id = :a"
        ),
        {"a": funded["wallet:alice"]},
    )
    session.commit()

    assert cli.main([]) == script.EXIT_FINDINGS

    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "balance_drift" in out
    assert "wallet:alice" in out


def test_unreachable_database_exits_two_not_one(
    cli, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The distinction that matters operationally.

    "The ledger is broken" and "the auditor is broken" need different responses.
    A job that returns the same code for both teaches people to ignore it, and
    an auditor that could not connect has NOT given the ledger a clean bill of
    health.
    """

    @contextmanager
    def exploding_scope() -> Iterator[Session]:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        yield  # pragma: no cover

    monkeypatch.setattr(script, "session_scope", exploding_scope)

    assert cli.main([]) == script.EXIT_ERROR
    assert "could not run" in capsys.readouterr().err


def test_json_output_is_machine_readable(
    cli, session: Session, funded: dict[str, uuid.UUID], capsys: pytest.CaptureFixture
) -> None:
    session.execute(
        text(
            "UPDATE account_balances SET posted_credits = posted_credits + 5000 "
            "WHERE account_id = :a"
        ),
        {"a": funded["wallet:alice"]},
    )
    session.commit()

    assert cli.main(["--json"]) == script.EXIT_FINDINGS

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["accounts_checked"] == 3
    assert payload["findings"][0]["check"] == "balance_drift"
    assert payload["findings"][0]["subject"] == "wallet:alice"


def test_report_does_not_leak_the_database_password(
    cli, funded: dict[str, uuid.UUID], capsys: pytest.CaptureFixture
) -> None:
    """The header names the target so you know what was audited.

    It prints only the host and database, never the credentials -- this output
    ends up in CI logs and alerting channels.
    """
    cli.main([])
    out = capsys.readouterr().out
    assert "miniledger" in out
    assert "miniledger:miniledger@" not in out
    assert "postgresql+psycopg://" not in out
