"""Strict USD cash evidence types — frozen Phase1 contract (writer A).

These values describe ONLY what was locally observed in one successful
broker ``account_balance`` response whose ``cash_infos`` carried exactly one
explicit ``USD`` entry:

* ``amount`` is that entry's ``available_cash`` — nothing else.
* ``request_started_at`` / ``request_completed_at`` are aware-UTC local
  timestamps bracketing the successful request (a retried attempt re-times
  its own request; failed attempts never contribute timestamps).
* ``provenance`` names the exact upstream field path the amount came from.

The upstream API exposes no server-side timestamp and no account
identifier, so a snapshot carries no claim about broker-side as-of
semantics, account identity, paper-versus-funded mode, or the absence of
borrowing/financing: ``available_cash`` is a field fact, not a solvency
proof. Consumers enforcing freshness or sufficiency do so on these locally
observed values alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True, slots=True)
class UsdCashSnapshot:
    """One locally observed, unambiguous, explicit-USD cash fact."""

    amount: Decimal
    currency: Literal["USD"]
    request_started_at: datetime
    request_completed_at: datetime
    provenance: Literal["account_balance.cash_infos.available_cash"]


class CashEvidenceUnavailable(RuntimeError):
    """No valid, unambiguous, explicit-USD cash fact could be observed.

    Raised when the broker response is missing a ``cash_infos`` USD entry,
    contains zero or several USD entries (ambiguity), or the single USD
    ``available_cash`` value is absent, malformed, non-finite, or negative.
    Also covers an untrustworthy request window (completion preceding
    start). Never used as a fallback signal: callers must fail closed.
    """
