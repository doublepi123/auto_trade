"""Quote entitlement monitoring — observation-only, never a trading decision.

Once the account's real-time quote package for a market lapses (e.g.
``US_QBBO_OpenAPI``), ``depth()`` fails with code 301604 and there is no
BBO: live entries fail closed and quote-dependent protective exits cannot
submit without a fresh trusted bid. Today nothing tells the operator — the
system just goes quiet. This module makes the entitlement state explicit
and visible.

**LongPort reports a rolling ~30-day window, not a fixed expiry.**
``quote_package_details()`` returns ``start = now − 1 day`` and
``end = now + 29 days`` for every package on every query (verified live:
two queries 81 s apart moved both bounds by 81 s). Consequences, by
design:

* ``days_left`` is "days to the broker-reported window end" — it rolls
  forward with each query and is NOT a confirmed expiry.
* ``EXPIRING`` only fires for a genuinely fixed-date window; it can never
  fire while the broker keeps rolling the window.
* A lapse cannot be inferred from the listing alone — we cannot verify how
  the listing changes when the entitlement actually lapses. The depth
  capability probe (``BrokerGateway.probe_depth_permission`` on the primary
  symbol) is the authoritative lapse signal: ``DENIED`` (301604) means the
  BBO capability is gone right now.

Design constraints (P0 live safety):

* Pure evaluation (:func:`assess_quote_entitlement`) plus a small
  observation tick (:meth:`QuoteEntitlementService.tick`). Neither changes
  any trading decision, pauses/resumes the runner, or touches risk or
  engine state.
* No DB writes. The only cached state is the last assessment result.
* Notification uses the runner's notifier ``notify_risk_event`` idiom with a
  once-per-UTC-day dedupe key per (market, status), so a 6h cadence
  produces one notification per day per status transition bucket.
  ``UNKNOWN`` is never notified (and never treated as OK) — a fetch error
  logs a WARNING through :class:`RepeatedLogThrottle` instead, so a
  long-lived outage still surfaces without spamming.
* Severity: ``EXPIRING`` → WARNING, ``MISSING`` → CRITICAL, with a message
  that says plainly why it matters: live entries and quote-dependent
  protective exits cannot operate without a real-time BBO package.
* Wording honesty: reasons describe the "broker-reported window end" and
  never claim "expires/ends in N days" as a fact.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace as dataclasses_replace
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Literal, cast

from app.core.broker import QuotePackage
from app.core.log_throttle import RepeatedLogThrottle

logger = logging.getLogger("auto_trade.quote_entitlement")

#: Markets whose real-time entitlement we can assess today.
_MARKETS = ("US", "HK")

#: US keys that grant a real-time BBO (best bid and offer).
_US_BBO_KEY_MARKERS = ("QBBO", "LV1", "L1")

#: HK keys that are index-only and therefore do NOT grant stock BBO.
_HK_INDEX_KEY_MARKERS = ("HangSengIndex", "Index")

#: How far before ``end_at`` the EXPIRING warning starts (inclusive).
WARN_DAYS_DEFAULT = 7


def is_realtime_quote_package(key: str, *, market: str) -> bool:
    """Whether a package key grants real-time quotes for ``market``.

    One small documented rule, shared by every caller:

    * The key must start with ``f"{market}_"`` — otherwise it belongs to
      another market (``CN_Connect`` is not a US entitlement).
    * US additionally requires a BBO-granting marker (``QBBO``/``LV1``/``L1``)
      because a US package without those markers does not carry best bid
      and offer.
    * HK excludes index-only keys (containing ``HangSengIndex`` or ``Index``):
      ``HK_HangSengIndex_AllTerminals`` quotes the index, not stock BBO.
    """
    if not key.startswith(f"{market}_"):
        return False
    if market == "US":
        return any(marker in key for marker in _US_BBO_KEY_MARKERS)
    if market == "HK":
        return not any(marker in key for marker in _HK_INDEX_KEY_MARKERS)
    return False


@dataclass(frozen=True)
class QuoteEntitlement:
    """Immutable assessment of one market's real-time quote entitlement.

    ``capability`` is the depth-probe outcome on the primary symbol — the
    authoritative lapse signal, since the package listing reports a rolling
    ~30-day window rather than a real expiry.
    """

    market: str
    status: Literal["OK", "EXPIRING", "MISSING", "UNKNOWN"]
    package_key: str
    end_at: datetime | None
    days_left: int | None
    reason: str
    capability: Literal["PERMITTED", "DENIED", "ERROR", "SKIPPED"] = "SKIPPED"


def _days_left(end_at: datetime, now: datetime) -> int:
    delta = end_at - now
    # Floor toward zero-ish whole days: a 7-day boundary counts as 7 and an
    # already-lapsed window counts as <= 0, both from total seconds so the
    # comparison in assess() stays consistent with this display value.
    return delta.days


def assess_quote_entitlement(
    packages: list[QuotePackage],
    *,
    market: str,
    now: datetime,
    warn_days: int = WARN_DAYS_DEFAULT,
) -> QuoteEntitlement:
    """Assess one market's real-time quote entitlement from the listing (pure).

    The listing reports a ROLLING ~30-day window (see module docstring), so
    this assessment alone cannot detect a lapse — the depth capability
    probe in :meth:`QuoteEntitlementService.tick` is authoritative. This
    function classifies what the listing says:

    * Among matching packages currently active (``start_at <= now < end_at``)
      the one with the latest ``end_at`` wins.
    * Active with ``end_at - now <= warn_days`` → ``EXPIRING`` — only
      meaningful for a genuinely fixed-date window; a rolling window never
      enters this branch.
    * A package without ``end_at`` is active from ``start_at`` and assesses
      as ``OK``.
    * No active matching package → ``MISSING`` (window reported as passed,
      not yet started, or no match at all) — a listing-based signal that
      the tick's capability probe may override.
    """
    if market not in _MARKETS:
        return QuoteEntitlement(
            market=market,
            status="UNKNOWN",
            package_key="",
            end_at=None,
            days_left=None,
            reason=f"unsupported market for entitlement assessment: {market}",
        )
    matching = [
        package
        for package in packages
        if is_realtime_quote_package(package.key, market=market)
    ]
    active = [
        package
        for package in matching
        if package.start_at is not None
        and package.start_at <= now
        and (package.end_at is None or now < package.end_at)
    ]
    if active:
        chosen = max(
            active,
            key=lambda p: (p.end_at is not None, p.end_at or now),
        )
        if chosen.end_at is None:
            return QuoteEntitlement(
                market=market,
                status="OK",
                package_key=chosen.key,
                end_at=None,
                days_left=None,
                reason="active package with no reported end date",
            )
        days = _days_left(chosen.end_at, now)
        if chosen.end_at - now <= timedelta(days=warn_days):
            return QuoteEntitlement(
                market=market,
                status="EXPIRING",
                package_key=chosen.key,
                end_at=chosen.end_at,
                days_left=days,
                reason=(
                    f"{chosen.key} broker-reported window ends "
                    f"{chosen.end_at.isoformat()} ({days} days to the "
                    f"broker-reported window end)"
                ),
            )
        return QuoteEntitlement(
            market=market,
            status="OK",
            package_key=chosen.key,
            end_at=chosen.end_at,
            days_left=days,
            reason=(
                f"{chosen.key} active; broker-reported window end "
                f"{chosen.end_at.isoformat()}"
            ),
        )
    # No active package: report the best-effort candidate for context.
    candidate = matching[0] if matching else None
    if candidate is None:
        return QuoteEntitlement(
            market=market,
            status="MISSING",
            package_key="",
            end_at=None,
            days_left=None,
            reason=f"no real-time quote package found for {market}",
        )
    if candidate.end_at is not None and candidate.end_at <= now:
        return QuoteEntitlement(
            market=market,
            status="MISSING",
            package_key=candidate.key,
            end_at=candidate.end_at,
            days_left=_days_left(candidate.end_at, now),
            reason=(
                f"{candidate.key} broker-reported window end has passed: "
                f"{candidate.end_at.isoformat()}"
            ),
        )
    if candidate.start_at is not None and candidate.start_at > now:
        return QuoteEntitlement(
            market=market,
            status="MISSING",
            package_key=candidate.key,
            end_at=candidate.end_at,
            days_left=(
                _days_left(candidate.end_at, now)
                if candidate.end_at is not None
                else None
            ),
            reason=(
                f"{candidate.key} has not started yet "
                f"(starts {candidate.start_at.isoformat()})"
            ),
        )
    return QuoteEntitlement(
        market=market,
        status="MISSING",
        package_key=candidate.key,
        end_at=candidate.end_at,
        days_left=(
            _days_left(candidate.end_at, now)
            if candidate.end_at is not None
            else None
        ),
        reason=f"{candidate.key} is not currently active",
    )


Capability = Literal["PERMITTED", "DENIED", "ERROR", "SKIPPED"]
"""Depth-probe outcome on the primary symbol (see ``QuoteEntitlement``)."""


class _EntitlementFetchError(RuntimeError):
    """Internal: broker fetch unsupported/failed → assess as UNKNOWN."""


def _impact_message() -> str:
    return (
        "live entries and quote-dependent protective exits cannot operate "
        "without a real-time BBO package"
    )


class QuoteEntitlementService:
    """Observation-only entitlement assessor with one cached last result.

    The tick fetches packages via the runner's broker, assesses the primary
    symbol's market (taken from the runner's current strategy params), caches
    the result, and notifies through the runner's notifier. It performs no
    DB writes, never pauses/resumes, and never touches risk or engine state.
    """

    def __init__(
        self,
        *,
        runner_factory: Callable[[], object] | None = None,
        warn_days: int = WARN_DAYS_DEFAULT,
    ) -> None:
        self._runner_factory = runner_factory
        self._warn_days = warn_days
        self._lock = threading.Lock()
        self._last_result: QuoteEntitlement | None = None
        self._notified_day_keys: set[tuple[str, str, date]] = set()
        self._unknown_log_throttle = RepeatedLogThrottle(window_seconds=3600.0)

    def tick(self, now: datetime | None = None) -> QuoteEntitlement:
        """Fetch, assess, probe capability, cache, and notify. Observation only."""
        now = now or datetime.now(timezone.utc)
        runner = self._get_runner()
        if runner is None:
            result = QuoteEntitlement(
                market="",
                status="UNKNOWN",
                package_key="",
                end_at=None,
                days_left=None,
                reason="runner is not available",
                capability="SKIPPED",
            )
            self._store(result)
            return result
        broker = getattr(runner, "broker", None)
        market = self._primary_market(runner)
        symbol = self._primary_symbol(runner)
        capability = self._probe_capability(broker, symbol)
        try:
            packages = self._fetch_packages(broker)
        except Exception as exc:
            result = QuoteEntitlement(
                market=market,
                status="UNKNOWN",
                package_key="",
                end_at=None,
                days_left=None,
                reason=f"fetch failed: {type(exc).__name__}",
                capability=capability,
            )
        else:
            listed = assess_quote_entitlement(
                packages, market=market, now=now, warn_days=self._warn_days
            )
            result = self._combine(listed, capability, symbol)
        self._store(result)
        self._notify(runner, result, now)
        return result

    @staticmethod
    def _combine(
        listed: QuoteEntitlement,
        capability: Capability,
        symbol: str,
    ) -> QuoteEntitlement:
        """Apply the capability probe's precedence over the listing assessment.

        * DENIED is the authoritative lapse signal: MISSING regardless of
          what the (rolling-window) listing claims.
        * PERMITTED means the BBO capability is present right now: OK even
          when the listing is absent — except a fixed-date EXPIRING window,
          which can still warn.
        * ERROR or SKIPPED leaves the listing-based assessment unchanged.
        """
        if capability == "DENIED":
            return dataclasses_replace(
                listed,
                status="MISSING",
                reason=(
                    f"depth() refused for {symbol}: no real-time quote "
                    f"permission (301604)"
                ),
                capability="DENIED",
            )
        if capability == "PERMITTED":
            if listed.status == "EXPIRING":
                return dataclasses_replace(
                    listed,
                    capability="PERMITTED",
                )
            if listed.status == "OK":
                suffix = (
                    f"; depth() permitted for {symbol}"
                    if symbol
                    else "; depth() permitted"
                )
                return dataclasses_replace(
                    listed,
                    reason=f"{listed.reason}{suffix}",
                    capability="PERMITTED",
                )
            if listed.status == "MISSING":
                # Listing says MISSING but the capability is present: the
                # listing cannot be trusted to detect lapses (rolling
                # window), so the capability wins.
                return dataclasses_replace(
                    listed,
                    status="OK",
                    reason=(
                        f"depth() permitted for {symbol}; package listing "
                        f"unreliable for lapse detection ({listed.reason})"
                    ),
                    capability="PERMITTED",
                )
            # UNKNOWN stays UNKNOWN — never treated as OK, even when the
            # probe is permitted; the fetch failure is the message.
            return dataclasses_replace(listed, capability="PERMITTED")
        return dataclasses_replace(listed, capability=capability)

    def _get_runner(self) -> object | None:
        factory = self._runner_factory
        if factory is not None:
            try:
                return factory()
            except Exception:
                logger.debug("runner factory failed", exc_info=True)
                return None
        try:
            from app.runner import peek_runner

            return peek_runner()
        except Exception:
            logger.debug("peek_runner failed", exc_info=True)
            return None

    @staticmethod
    def _fetch_packages(broker: object) -> list[QuotePackage]:
        """Fetch packages from the runner's broker; raise on any failure."""
        fetch = getattr(broker, "get_quote_packages", None)
        if not callable(fetch):
            raise _EntitlementFetchError(
                "broker does not support quote package details"
            )
        return cast(list[QuotePackage], fetch())

    @staticmethod
    def _primary_market(runner: object) -> str:
        """Primary market from the runner's current strategy params."""
        try:
            params = getattr(getattr(runner, "engine", None), "params", None)
            return str(getattr(params, "market", "") or "")
        except Exception:
            logger.debug("primary market lookup failed", exc_info=True)
            return ""

    @staticmethod
    def _primary_symbol(runner: object) -> str:
        """Primary symbol from the runner's current strategy params."""
        try:
            params = getattr(getattr(runner, "engine", None), "params", None)
            return str(getattr(params, "symbol", "") or "")
        except Exception:
            logger.debug("primary symbol lookup failed", exc_info=True)
            return ""

    @staticmethod
    def _probe_capability(broker: object, symbol: str) -> Capability:
        """Probe depth permission on the primary symbol (single call).

        Empty symbol → ``SKIPPED``. The broker method itself returns
        ``PERMITTED | DENIED | ERROR``; a missing method or a raising
        wrapper is an ``ERROR``, never a silent OK.
        """
        if not symbol:
            return "SKIPPED"
        probe = getattr(broker, "probe_depth_permission", None)
        if not callable(probe):
            return "ERROR"
        try:
            return cast(Capability, str(probe(symbol)))
        except Exception:
            logger.debug("depth permission probe raised", exc_info=True)
            return "ERROR"

    def _store(self, result: QuoteEntitlement) -> None:
        with self._lock:
            self._last_result = result

    def last_result(self) -> QuoteEntitlement | None:
        with self._lock:
            return self._last_result

    def _notify(
        self,
        runner: object,
        result: QuoteEntitlement,
        now: datetime,
    ) -> None:
        if result.status == "UNKNOWN":
            # UNKNOWN is never treated as OK and never notified; log a
            # throttled WARNING so a long outage still surfaces.
            if self._unknown_log_throttle.should_log(
                f"UNKNOWN:{result.market}"
            ):
                suppressed = self._unknown_log_throttle.take_suppressed_count()
                logger.warning(
                    "quote entitlement UNKNOWN for market=%s (%s); "
                    "assumed not OK; suppressed=%d",
                    result.market,
                    result.reason,
                    suppressed,
                )
            return
        if result.status == "OK":
            return
        day = now.astimezone(timezone.utc).date()
        key = (result.market, result.status, day)
        with self._lock:
            if key in self._notified_day_keys:
                return
            self._notified_day_keys.add(key)
        severity = (
            "CRITICAL" if result.status == "MISSING" else "WARNING"
        )
        event_type = f"QUOTE_ENTITLEMENT_{result.status}"
        detail = result.reason or "real-time quote package problem"
        message = (
            f"{detail}; {_impact_message()}"
        )
        try:
            notifier = getattr(runner, "notifier", None)
            notify = getattr(notifier, "notify_risk_event", None)
            if callable(notify):
                notify(event_type, message, severity=severity)
        except Exception:
            logger.debug("quote entitlement notification failed", exc_info=True)


# --- module singleton ------------------------------------------------------

_singleton: QuoteEntitlementService | None = None
_singleton_lock = threading.Lock()


def get_quote_entitlement_service() -> QuoteEntitlementService:
    """Return the process-local shared QuoteEntitlementService."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = QuoteEntitlementService()
        return _singleton


def set_quote_entitlement_service(
    service: QuoteEntitlementService | None,
) -> None:
    """Override the shared service (tests only). Pass ``None`` to reset."""
    global _singleton
    with _singleton_lock:
        _singleton = service
