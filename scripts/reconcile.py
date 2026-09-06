"""Reconciliation job. Run it on a schedule; alert on a non-zero exit.

    python -m scripts.reconcile           # human-readable report
    python -m scripts.reconcile --json    # machine-readable, for monitoring

Exit codes, chosen so this drops straight into cron or a CI step:

    0   the cache agrees with the log and the log is internally consistent
    1   at least one finding -- something is wrong, look at the output
    2   the job could not run at all (database unreachable, etc.)

The distinction between 1 and 2 matters operationally. "The ledger is broken"
and "the auditor is broken" demand completely different responses, and a job
that returns the same code for both trains people to ignore it.

Read-only: this job diagnoses, it never repairs. Auto-correcting a balance
would destroy the evidence needed to work out why it drifted, and if the cause
were a bug still in flight it would paper over it on every run. Repair is a
human decision, and it is made by posting a compensating transfer -- never by
editing history.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db import session_scope
from app.reconciliation import ReconciliationReport, reconcile

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def _redacted_target() -> str:
    """Database host/name for the report header, without the password."""
    url = get_settings().database_url
    tail = url.rsplit("@", 1)[-1]
    return tail or "(unknown)"


def render(report: ReconciliationReport) -> str:
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    lines = [
        "MiniLedger reconciliation",
        f"  run at    {stamp}",
        f"  database  {_redacted_target()}",
        "",
        f"  accounts  {report.accounts_checked}",
        f"  transfers {report.transfers_checked}",
        f"  entries   {report.entries_checked}",
        "",
    ]

    if report.ok:
        lines.append("PASS - every balance is supported by the entry log.")
        return "\n".join(lines)

    noun = "finding" if len(report.findings) == 1 else "findings"
    lines.append(f"FAIL - {len(report.findings)} {noun}")
    lines.append("")

    for finding in report.findings:
        lines.append(f"[{finding.check}] {finding.subject}")
        lines.append(f"  {finding.summary}")
        lines += [f"  {detail}" for detail in finding.details]
        lines.append("")

    lines.append(
        "Balances are NOT repaired automatically. Investigate the entries named above;"
    )
    lines.append(
        "correct a genuine error by posting a compensating transfer, never by "
        "editing history."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.reconcile",
        description="Re-derive every balance from the ledger and report drift.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of a human-readable report",
    )
    args = parser.parse_args(argv)

    try:
        with session_scope() as session:
            report = reconcile(session)
    except SQLAlchemyError as exc:
        # Deliberately a different exit code from "found drift". An auditor
        # that cannot reach the database has not given the ledger a clean bill
        # of health, and must never be mistaken for one that did.
        print(f"reconciliation could not run: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        print(render(report))

    return EXIT_OK if report.ok else EXIT_FINDINGS


if __name__ == "__main__":
    raise SystemExit(main())
