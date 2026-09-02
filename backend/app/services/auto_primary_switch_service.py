"""Automatic primary-symbol switching driven by range-strategy fitness.

DEFAULT OFF. Enabling this deliberately relaxes the repository's standing rule
that candidate-pool and shadow evidence never switch the live trading symbol
(see README: "不会自动切换主交易标的"). It promotes on fitness evidence alone
and does NOT require the forward profit evidence that
``/api/universe/promotion-readiness`` demands, so a promoted symbol may have no
closed-trade track record.

Two invariants bound the blast radius:

* Only symbols the latest completed selection run marked ``selected`` are
  eligible, so an arbitrary symbol can never become the live primary.
* The switch is executed through ``AppRunner.assert_primary_switch_safe``,
  which refuses while a position, pending order, in-flight trigger, unresolved
  reconciliation, or non-FLAT engine state exists. A switch therefore never
  abandons live exposure.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import event as sa_event, select
from sqlalchemy.orm import Session

from app.config import settings
from app.domain.strategy_v2.signal_edge import (
    DEFAULT_MIN_DISTINCT_DAYS,
    DEFAULT_MIN_RESOLVED_TRADES,
    VERDICT_INSUFFICIENT_DATA,
    VERDICT_PASS,
)
from app.models import (
    StrategyConfig,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
)
from app.services.range_fitness_service import (
    RangeFitnessRow,
    RangeFitnessService,
    VERDICT_RANGE_SUITABLE,
)
from app.services.signal_edge_service import SignalEdgeService
from app.services.trade_event_service import (
    encode_event_payload,
    record_trade_event,
)

logger = logging.getLogger("auto_trade.auto_primary_switch")

OUTCOME_DISABLED = "DISABLED"
OUTCOME_NO_PRIMARY = "NO_PRIMARY_CONFIGURED"
OUTCOME_INCUMBENT_ACCEPTABLE = "INCUMBENT_ACCEPTABLE"
OUTCOME_INCUMBENT_EVIDENCE_THIN = "INCUMBENT_EVIDENCE_THIN"
OUTCOME_NO_ELIGIBLE_CANDIDATE = "NO_ELIGIBLE_CANDIDATE"
OUTCOME_SIGNAL_EDGE_UNPROVEN = "SIGNAL_EDGE_UNPROVEN"
OUTCOME_SWITCH_BLOCKED = "SWITCH_BLOCKED"
OUTCOME_SWITCHED = "SWITCHED"

# Durable provenance vocabulary. Operators grep these, so they must stay stable.
EVENT_PRIMARY_SWITCHED = "PRIMARY_SWITCHED"
EVENT_PRIMARY_SWITCH_ROLLED_BACK = "PRIMARY_SWITCH_ROLLED_BACK"
EVENT_PRIMARY_SWITCH_BLOCKED = "PRIMARY_SWITCH_BLOCKED"
AUDIT_PRIMARY_SWITCH = "AUTO_PRIMARY_SWITCH"


@dataclass(frozen=True)
class AutoPrimarySwitchResult:
    outcome: str
    incumbent: str = ""
    incumbent_trend_pct: float | None = None
    candidate: str = ""
    candidate_trend_pct: float | None = None
    detail: str = ""
    signal_edge_unassessable: bool = False


@dataclass(frozen=True, slots=True)
class _SignalEdgeBlock:
    detail: str
    unassessable: bool


@dataclass(frozen=True, slots=True)
class _SwitchPlan:
    """The change a switch applies, plus the evidence that earned it."""

    symbol: str
    market: str
    buy_low: float
    sell_high: float
    reference_price: float
    reference_bar_at: datetime | None
    incumbent_trend_pct: float
    candidate_trend_pct: float


def _as_utc(value: datetime) -> datetime:
    """Read a naive datetime as the UTC instant it was written as.

    SQLite drops the offset on ``DateTime(timezone=True)`` columns, so a stored
    UTC timestamp comes back naive and comparing it to an aware anchor raises.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _reference_age_seconds(bar_at: datetime | None, at: datetime) -> float | None:
    return None if bar_at is None else (at - _as_utc(bar_at)).total_seconds()


def _reference_is_fresh(age_seconds: float | None) -> bool:
    """Fresh means measurable and inside the bound on BOTH sides.

    A future-dated bar yields a negative age that satisfies any upper bound,
    yet a close the market has not printed yet is clock-skewed or corrupt, not
    fresh. Unmeasurable and future-dated ages both fail closed.
    """
    if age_seconds is None:
        return False
    return 0 <= age_seconds <= settings.auto_primary_switch_max_price_age_seconds


class AutoPrimarySwitchService:
    def __init__(
        self,
        db: Session,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._db = db
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def evaluate(
        self,
        runner: Any,
        *,
        now: datetime | None = None,
    ) -> AutoPrimarySwitchResult:
        if not settings.auto_primary_switch_enabled:
            return AutoPrimarySwitchResult(OUTCOME_DISABLED)

        anchor = _as_utc(now or self._clock())

        config = self._db.scalar(
            select(StrategyConfig).order_by(StrategyConfig.id.desc())
        )
        incumbent = (config.symbol or "").strip().upper() if config else ""
        if not incumbent:
            return AutoPrimarySwitchResult(OUTCOME_NO_PRIMARY)

        rows = RangeFitnessService(self._db).assess(
            lookback_days=settings.auto_primary_switch_lookback_days,
            min_samples=settings.auto_primary_switch_min_samples,
            trend_unsuitable_pct=(
                settings.auto_primary_switch_incumbent_trend_pct
            ),
            range_suitable_pct=(
                settings.auto_primary_switch_candidate_trend_pct
            ),
            reach_lookback_days=(
                settings.auto_primary_switch_reach_lookback_days
            ),
            now=anchor,
        )
        by_symbol = {row.symbol: row for row in rows}

        incumbent_row = by_symbol.get(incumbent)
        if incumbent_row is None or incumbent_row.samples < settings.auto_primary_switch_min_samples:
            return AutoPrimarySwitchResult(
                OUTCOME_INCUMBENT_EVIDENCE_THIN,
                incumbent=incumbent,
                detail="incumbent has insufficient fitness evidence",
            )
        if incumbent_row.trend_blocked_pct < settings.auto_primary_switch_incumbent_trend_pct:
            return AutoPrimarySwitchResult(
                OUTCOME_INCUMBENT_ACCEPTABLE,
                incumbent=incumbent,
                incumbent_trend_pct=incumbent_row.trend_blocked_pct,
            )

        # Both promotion criteria above -- trend share and reach-rate -- read
        # Strategy v2 shadow evidence. A signal with no directional information
        # makes that evidence a ranking of noise, so switching on it relocates
        # losses rather than avoiding them. Checked only once a switch is on the
        # table, keeping the common incumbent-acceptable path free of the query.
        edge_blocked = self._signal_edge_block_reason()
        if edge_blocked is not None:
            return AutoPrimarySwitchResult(
                OUTCOME_SIGNAL_EDGE_UNPROVEN,
                incumbent=incumbent,
                incumbent_trend_pct=incumbent_row.trend_blocked_pct,
                detail=edge_blocked.detail,
                signal_edge_unassessable=edge_blocked.unassessable,
            )

        eligible = self._eligible_candidates()
        best = None
        for row in rows:
            if row.symbol == incumbent or row.symbol not in eligible:
                continue
            if row.verdict != VERDICT_RANGE_SUITABLE:
                continue
            if row.trend_blocked_pct > settings.auto_primary_switch_candidate_trend_pct:
                continue
            # Without a reference price the new symbol would inherit the old
            # symbol's interval, which sits nowhere near its price and would
            # never trigger — a switch into an immediately dead interval.
            if row.last_close_price is None or row.last_close_price <= 0:
                continue
            # A close from an earlier session prices a band the live market can
            # no longer reach. Any bar inside the multi-day fitness window used
            # to qualify, so a stale close once centred a live band 1.5% below
            # the market and took zero fills for 28 days. Fails closed: a close
            # whose age cannot be measured is not a proven-fresh one.
            reference_age = _reference_age_seconds(row.last_bar_at, anchor)
            if not _reference_is_fresh(reference_age):
                logger.warning(
                    "automatic primary switch rejected %s: reference close at "
                    "%s is %s, outside the 0-%ss freshness window",
                    row.symbol,
                    row.last_bar_at,
                    (
                        "unknown age"
                        if reference_age is None
                        else f"{reference_age:.0f}s old"
                    ),
                    settings.auto_primary_switch_max_price_age_seconds,
                )
                continue
            # Reach-rate gate. A low ADX trend share only says price is not
            # trending; it does not say the swings are big enough to clear the
            # round-trip cost. Measured over 247 closed shadow trades, reach-rate
            # separated winners from losers with no exceptions (85% vs 22%) while
            # trend share ranked them barely better than chance -- its top-ranked
            # symbol was a net loser. Require BOTH so a quiet-but-too-tight
            # symbol can never be promoted on trend share alone.
            if not self._reach_rate_ok(row):
                continue
            if best is None or row.trend_blocked_pct < best.trend_blocked_pct:
                best = row
        if best is None:
            return AutoPrimarySwitchResult(
                OUTCOME_NO_ELIGIBLE_CANDIDATE,
                incumbent=incumbent,
                incumbent_trend_pct=incumbent_row.trend_blocked_pct,
                detail=(
                    "no selected candidate is range-suitable with sufficient "
                    "reach evidence"
                ),
            )

        market = eligible[best.symbol]
        try:
            runner.assert_primary_switch_safe(best.symbol, market)
        except Exception as exc:
            blocked = AutoPrimarySwitchResult(
                OUTCOME_SWITCH_BLOCKED,
                incumbent=incumbent,
                incumbent_trend_pct=incumbent_row.trend_blocked_pct,
                candidate=best.symbol,
                candidate_trend_pct=best.trend_blocked_pct,
                detail=str(exc),
            )
            self._record_block(blocked)
            return blocked

        reference_price = float(best.last_close_price or 0)
        half_width = reference_price * (
            settings.llm_interval_volatility_threshold_pct / 100
        )
        buy_low = round(reference_price - half_width, 4)
        sell_high = round(reference_price + half_width, 4)
        if buy_low <= 0 or sell_high <= buy_low:
            return AutoPrimarySwitchResult(
                OUTCOME_SWITCH_BLOCKED,
                incumbent=incumbent,
                incumbent_trend_pct=incumbent_row.trend_blocked_pct,
                candidate=best.symbol,
                candidate_trend_pct=best.trend_blocked_pct,
                detail="cannot derive a valid interval for the candidate",
            )

        # Re-measured against a fresh clock reading. assert_primary_switch_safe
        # performs a blocking broker position probe, so the reference can age
        # past the bound between the early gate and this write; the early gate
        # is only a cheap rejection ahead of that probe, and this is the
        # authoritative one. The band must be built from a reference that is
        # still fresh at the instant it becomes durable.
        if not _reference_is_fresh(
            _reference_age_seconds(best.last_bar_at, _as_utc(self._clock()))
        ):
            blocked = AutoPrimarySwitchResult(
                OUTCOME_SWITCH_BLOCKED,
                incumbent=incumbent,
                incumbent_trend_pct=incumbent_row.trend_blocked_pct,
                candidate=best.symbol,
                candidate_trend_pct=best.trend_blocked_pct,
                detail="reference price went stale before the switch was written",
            )
            self._record_block(blocked)
            return blocked

        try:
            self._commit_switch(
                runner,
                config,
                _SwitchPlan(
                    symbol=best.symbol,
                    market=market,
                    buy_low=buy_low,
                    sell_high=sell_high,
                    reference_price=reference_price,
                    reference_bar_at=best.last_bar_at,
                    incumbent_trend_pct=incumbent_row.trend_blocked_pct,
                    candidate_trend_pct=best.trend_blocked_pct,
                ),
            )
        except Exception as exc:
            logger.exception("automatic primary switch failed to persist")
            return AutoPrimarySwitchResult(
                OUTCOME_SWITCH_BLOCKED,
                incumbent=incumbent,
                incumbent_trend_pct=incumbent_row.trend_blocked_pct,
                candidate=best.symbol,
                candidate_trend_pct=best.trend_blocked_pct,
                detail=f"persist failed: {exc}",
            )

        logger.warning(
            "automatic primary switch: %s (trend %.2f%%) -> %s (trend %.2f%%)",
            incumbent,
            incumbent_row.trend_blocked_pct,
            best.symbol,
            best.trend_blocked_pct,
        )
        return AutoPrimarySwitchResult(
            OUTCOME_SWITCHED,
            incumbent=incumbent,
            incumbent_trend_pct=incumbent_row.trend_blocked_pct,
            candidate=best.symbol,
            candidate_trend_pct=best.trend_blocked_pct,
            detail=market,
        )

    def _commit_switch(
        self,
        runner: Any,
        config: Any,
        plan: _SwitchPlan,
    ) -> None:
        """Persist the new primary symbol, its provenance, and reload the runner.

        The safety gate is already proven by the caller's
        ``assert_primary_switch_safe`` call, so this deliberately does not go
        through ``update_strategy_with_runtime_reload`` — that helper re-runs the
        gate against the process-global runner, which would both duplicate the
        check and ignore the runner this service was handed.

        A reload failure rolls the symbol back so the live engine is never left
        pointing at a half-applied config.
        """
        from app.services.strategy_service import StrategyService

        svc = StrategyService(self._db)
        previous_symbol = (config.symbol or "").strip().upper()
        previous_market = (config.market or "US").strip().upper()
        previous_buy_low = float(config.buy_low or 0)
        previous_sell_high = float(config.sell_high or 0)
        payload: dict[str, Any] = {
            "previous_symbol": previous_symbol,
            "new_symbol": plan.symbol,
            "previous_market": previous_market,
            "new_market": plan.market,
            "previous_buy_low": previous_buy_low,
            "previous_sell_high": previous_sell_high,
            "new_buy_low": plan.buy_low,
            "new_sell_high": plan.sell_high,
            "reference_price": plan.reference_price,
            "reference_bar_at": (
                None
                if plan.reference_bar_at is None
                else _as_utc(plan.reference_bar_at).isoformat()
            ),
            "incumbent_trend_pct": plan.incumbent_trend_pct,
            "candidate_trend_pct": plan.candidate_trend_pct,
        }
        reload_strategy = getattr(runner, "reload_strategy", None)
        # Staged BEFORE update_config, which commits the session itself. Staging
        # afterwards would leave a crash window in which the live symbol has
        # changed with no record of why — the original defect, merely narrowed.
        event = record_trade_event(
            self._db,
            event_type=EVENT_PRIMARY_SWITCHED,
            symbol=plan.symbol,
            status=OUTCOME_SWITCHED,
            message=(
                f"automatic primary switch {previous_symbol} -> {plan.symbol}"
            ),
            payload=payload,
        )
        self._apply_config(svc, {
            "symbol": plan.symbol,
            "market": plan.market,
            "buy_low": plan.buy_low,
            "sell_high": plan.sell_high,
        })
        try:
            # Age re-read AFTER the commit returned, not before it. update_config
            # takes the SQLite writer lock before committing and that wait is
            # unbounded, so no earlier reading can bound the age at commit time.
            # Age only grows, so commit_age <= this reading: inside the bound
            # here proves the commit was inside it, and outside proves nothing.
            # An unproven band is undone through the rollback path below rather
            # than left in effect, and the reload never sees it.
            committed_age = _reference_age_seconds(
                plan.reference_bar_at, _as_utc(self._clock())
            )
            if not _reference_is_fresh(committed_age):
                raise RuntimeError(
                    "reference price was stale when the switch committed (age "
                    + (
                        "unknown"
                        if committed_age is None
                        else f"{committed_age:.0f}s"
                    )
                    + f", bound {settings.auto_primary_switch_max_price_age_seconds}s)"
                )
            if callable(reload_strategy):
                reload_strategy()
        except Exception as exc:
            logger.exception(
                "automatic primary switch could not be completed; "
                "rolling back to %s",
                previous_symbol,
            )
            # The switch is being undone, so the committed row must stop
            # claiming it stands. Restated rather than deleted, and made durable
            # by the restoring update_config below.
            event.event_type = EVENT_PRIMARY_SWITCH_ROLLED_BACK
            event.status = "ROLLED_BACK"
            event.message = (
                f"automatic primary switch to {plan.symbol} rolled back to "
                f"{previous_symbol}: {exc}"
            )
            event.payload_json = encode_event_payload(
                {**payload, "rollback_reason": str(exc)}
            )
            self._db.add(event)
            self._apply_config(svc, {
                "symbol": previous_symbol,
                "market": previous_market,
                "buy_low": previous_buy_low,
                "sell_high": previous_sell_high,
            })
            try:
                if callable(reload_strategy):
                    reload_strategy()
            except Exception:
                logger.critical(
                    "automatic primary switch rollback reload failed",
                    exc_info=True,
                )
            raise
        self._record_switch_audit(payload)

    def _apply_config(self, svc: Any, values: dict[str, Any]) -> None:
        """Apply a config change, tolerating a failure that happened AFTER commit.

        ``StrategyService.update_config`` commits and only then refreshes, so a
        raise from it does not mean the write was rejected. Whether the commit
        landed is read from an ``after_commit`` hook rather than a verification
        query: a query is itself fallible, and one that raised here would
        propagate before either reload or rollback ran, stranding the exact
        state this path exists to prevent. When the change did land the caller
        must continue into its reload/rollback handling, or the live engine is
        left on the old symbol while the database claims the new one.

        The session is always left with no open transaction. A commit's
        post-steps, or a failure among them, otherwise leave a read snapshot
        open that the restore would upgrade into a write -- the SQLite
        ``BUSY_SNAPSHOT`` failure mode.
        """
        committed: list[bool] = []

        def _mark_committed(_session: Session) -> None:
            committed.append(True)

        sa_event.listen(self._db, "after_commit", _mark_committed)
        try:
            svc.update_config(values)
        except Exception:
            if not committed:
                raise
            logger.exception(
                "automatic primary switch config write raised after it "
                "committed; continuing so the reload path still runs",
            )
        finally:
            sa_event.remove(self._db, "after_commit", _mark_committed)
            self._db.rollback()

    def _record_block(self, blocked: AutoPrimarySwitchResult) -> None:
        """Persist a refused switch so an operator sees the system tried.

        Best-effort: the veto is already decided, and a write that loses the
        SQLite writer lock to the live trading loop must not turn a clean
        refusal into a crash.
        """
        try:
            record_trade_event(
                self._db,
                event_type=EVENT_PRIMARY_SWITCH_BLOCKED,
                symbol=blocked.candidate,
                status=OUTCOME_SWITCH_BLOCKED,
                message=(
                    f"automatic primary switch {blocked.incumbent} -> "
                    f"{blocked.candidate} refused: {blocked.detail}"
                ),
                payload={
                    "previous_symbol": blocked.incumbent,
                    "candidate_symbol": blocked.candidate,
                    "incumbent_trend_pct": blocked.incumbent_trend_pct,
                    "candidate_trend_pct": blocked.candidate_trend_pct,
                    "reason": blocked.detail,
                },
            )
            self._db.commit()
        except Exception:
            self._db.rollback()
            logger.warning(
                "failed to record blocked automatic primary switch",
                exc_info=True,
            )

    def _record_switch_audit(self, payload: dict[str, Any]) -> None:
        """Mirror the switch into the audit log, following the swallow-and-warn
        convention: provenance must never veto the operation it records."""
        from app.api.deps import init_audit_logger

        try:
            init_audit_logger().record(
                AUDIT_PRIMARY_SWITCH,
                severity="WARNING",
                request_summary=payload,
            )
        except Exception as exc:
            logger.warning("audit_record_failed: %s", exc)

    def _signal_edge_block_reason(self) -> _SignalEdgeBlock | None:
        """Return why a switch must be blocked, or ``None`` when edge is proven.

        Assessed across all symbols, not just the candidate: first-passage tests
        whether the entry rule carries directional information, and that rule is
        shared. Per-symbol samples are also far too thin to resolve -- symbols
        accumulate single-digit closed trades over the weeks needed to reach the
        evidence floor, so a per-symbol gate would report INSUFFICIENT_DATA
        forever and could never be satisfied.

        Fails closed: an unassessable signal is not a proven one, and promoting
        on absent evidence is exactly what made trend share alone unsafe.
        """
        if not settings.auto_primary_switch_require_signal_edge:
            return None
        try:
            verdict, _stop, _target, _symbol = SignalEdgeService(self._db).assess()
        except ValueError as exc:
            return _SignalEdgeBlock(
                detail=f"signal edge not assessable: {exc}",
                unassessable=True,
            )
        if verdict.verdict == VERDICT_PASS:
            return None
        detail = "; ".join(verdict.reasons)
        first_passage = verdict.first_passage
        provenance_unassessable = first_passage.matched_versions == 0 or (
            first_passage.matched_trades == 0
            and first_passage.provenance_excluded_trades > 0
        )
        pnl_unassessable = (
            verdict.verdict == VERDICT_INSUFFICIENT_DATA
            and first_passage.missing_pnl_excluded > 0
            and (
                verdict.gross.observations < DEFAULT_MIN_RESOLVED_TRADES
                or verdict.gross.distinct_days < DEFAULT_MIN_DISTINCT_DAYS
                or verdict.net.observations < DEFAULT_MIN_RESOLVED_TRADES
                or verdict.net.distinct_days < DEFAULT_MIN_DISTINCT_DAYS
            )
        )
        return _SignalEdgeBlock(
            detail=(
                f"shadow signal edge {verdict.verdict}"
                + (f": {detail}" if detail else "")
            ),
            unassessable=provenance_unassessable or pnl_unassessable,
        )

    @staticmethod
    def _reach_rate_ok(row: RangeFitnessRow) -> bool:
        """Require measured reach evidence at or above the configured floor.

        A symbol with no closed shadow trades yet has ``reach_rate_pct is None``
        and is rejected: promoting on absent evidence is what made trend share
        alone unsafe. The closed-trade floor keeps a single lucky trade from
        reading as a 100% reach-rate.
        """
        if row.reach_rate_pct is None:
            return False
        if row.closed_trades < settings.auto_primary_switch_min_closed_trades:
            return False
        return row.reach_rate_pct >= settings.auto_primary_switch_min_reach_rate_pct

    def _eligible_candidates(self) -> dict[str, str]:
        run = self._db.scalar(
            select(UniverseSelectionRun)
            .where(UniverseSelectionRun.status == "COMPLETE")
            .order_by(UniverseSelectionRun.id.desc())
        )
        if run is None:
            return {}
        rows = self._db.execute(
            select(
                UniverseSelectionCandidate.symbol,
                UniverseSelectionCandidate.market,
            ).where(
                UniverseSelectionCandidate.run_id == run.id,
                UniverseSelectionCandidate.selected.is_(True),
            )
        ).all()
        return {
            str(symbol).strip().upper(): str(market or "US").strip().upper()
            for symbol, market in rows
            if str(symbol or "").strip()
        }
