"""Conditional alert rules — user-defined thresholds evaluated by a cron.

Broker/notifier-agnostic: ``evaluate`` reads live quotes (price rules) and
the active ``RuntimeState`` (daily_loss / consecutive_losses /
kill_switch_engaged rules) via an injected runner and dispatches through its
notifier, respecting a per-rule cooldown. Never touches the live order path
— it only reads and notifies.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AlertFiring, AlertRule, RuntimeState, StrategyConfig
from app.schemas import (
    AlertEvaluateResult,
    AlertRuleCreate,
    AlertRuleEffectiveness,
    AlertRuleOut,
)

logger = logging.getLogger(__name__)

PRICE_RULES = {"price_above", "price_below"}
# Needs a live quote AND the active StrategyConfig interval, so it is quoted
# like a price rule but projected as a deviation percentage instead of a price.
INTERVAL_RULES = {"interval_stale"}
QUOTED_RULES = PRICE_RULES | INTERVAL_RULES
# Account-wide-only rule types that read the authoritative account state
# (latest StrategyConfig symbol -> that symbol's RuntimeState, falling back to
# the legacy ``symbol == ""`` row). They never bind to a secondary symbol's
# row and never trigger a broker quote fetch.
ACCOUNT_RULES = {"consecutive_losses", "kill_switch_engaged"}
# Account-wide rules sourced from the BROKER's margin/financing snapshot rather
# than local RuntimeState. They need one ``get_account()`` call per tick, so the
# fetch is lazy: it only happens when such a rule is enabled.
BROKER_ACCOUNT_RULES = {"margin_risk_level", "margin_call"}
# All rule types that read ``RuntimeState`` instead of live broker quotes.
STATE_RULES = {"daily_loss"} | ACCOUNT_RULES


class _NotifierLike(Protocol):
    def send(self, title: str, content: str, severity: str = "INFO") -> bool: ...


class AlertRuleService:
    def __init__(self, db: Session) -> None:
        self._db = db

    # --- CRUD -------------------------------------------------------------

    def create(self, payload: AlertRuleCreate) -> AlertRuleOut:
        rule = AlertRule(
            name=payload.name.strip(),
            symbol=(payload.symbol or "").strip().upper(),
            rule_type=payload.rule_type,
            threshold=float(payload.threshold),
            severity=payload.severity,
            enabled=payload.enabled,
            cooldown_seconds=int(payload.cooldown_seconds),
        )
        self._db.add(rule)
        self._db.commit()
        self._db.refresh(rule)
        return self._to_out(rule)

    def list_rules(self, *, enabled_only: bool = False) -> list[AlertRuleOut]:
        stmt = select(AlertRule).order_by(AlertRule.id.desc())
        if enabled_only:
            stmt = stmt.where(AlertRule.enabled.is_(True))
        return [self._to_out(r) for r in self._db.scalars(stmt)]

    def get(self, rule_id: int) -> AlertRuleOut | None:
        rule = self._db.get(AlertRule, rule_id)
        return self._to_out(rule) if rule is not None else None

    def update(self, rule_id: int, payload: AlertRuleCreate) -> AlertRuleOut | None:
        rule = self._db.get(AlertRule, rule_id)
        if rule is None:
            return None
        rule.name = payload.name.strip()
        rule.symbol = (payload.symbol or "").strip().upper()
        rule.rule_type = payload.rule_type
        rule.threshold = float(payload.threshold)
        rule.severity = payload.severity
        rule.enabled = payload.enabled
        rule.cooldown_seconds = int(payload.cooldown_seconds)
        self._db.commit()
        self._db.refresh(rule)
        return self._to_out(rule)

    def delete(self, rule_id: int) -> bool:
        rule = self._db.get(AlertRule, rule_id)
        if rule is None:
            return False
        self._db.delete(rule)
        self._db.commit()
        return True

    # --- Evaluation -------------------------------------------------------

    def evaluate(self, runner: Any, *, now: datetime | None = None) -> AlertEvaluateResult:
        now = now or datetime.now(timezone.utc)
        quote_provider = getattr(runner, "broker", None)
        notifier: _NotifierLike | None = getattr(runner, "notifier", None)

        rules = list(self._db.scalars(select(AlertRule).where(AlertRule.enabled.is_(True))))
        symbols = {r.symbol for r in rules if r.rule_type in QUOTED_RULES and r.symbol}
        if any(r.rule_type in INTERVAL_RULES and not r.symbol for r in rules):
            primary = self._interval_symbol(
                AlertRule(symbol="", rule_type="interval_stale")
            )
            if primary:
                symbols.add(primary)
        quote_map = _fetch_quotes(quote_provider, sorted(symbols))
        margin_infos = (
            _fetch_margin_infos(quote_provider)
            if any(r.rule_type in BROKER_ACCOUNT_RULES for r in rules)
            else None
        )

        fired = 0
        skipped_cooldown = 0
        # Capture firing payloads (scalars only) as rules fire; persisted after
        # the main commit so a firing-log write can never poison the
        # last_fired_at update transaction.
        firing_payloads: list[dict[str, Any]] = []
        for rule in rules:
            # Isolate each rule: one bad rule (DB error, unexpected value) must
            # not abort the whole tick or drop earlier rules' last_fired_at
            # updates. The trailing commit still runs so successful rules persist.
            try:
                if not _eligible(rule, now):
                    skipped_cooldown += 1
                    continue
                value = self._current_value(rule, quote_map, margin_infos)
                if value is None:
                    continue  # data unavailable (no quote / no state) — skip silently
                resolved_symbol = (
                    self._interval_symbol(rule) or ""
                    if rule.rule_type in INTERVAL_RULES
                    else str(rule.symbol or "")
                )
                triggered, message = _check(rule, value, resolved_symbol)
                if triggered and notifier is not None:
                    title = f"告警 · {rule.name}"
                    try:
                        ok = bool(notifier.send(title, message, severity=rule.severity or "WARNING"))
                    except Exception:
                        logger.exception("alert rule %s notification failed", rule.id)
                        ok = False
                    if ok:
                        rule.last_fired_at = now
                        fired += 1
                        firing_payloads.append({
                            "rule_id": int(rule.id),
                            "symbol": resolved_symbol,
                            "rule_type": str(rule.rule_type or ""),
                            "threshold": float(rule.threshold),
                            "trigger_value": float(value),
                            "severity": str(rule.severity or "WARNING"),
                            "message": str(message or ""),
                            "fired_at": now,
                        })
            except Exception:
                logger.exception("alert rule %s evaluation failed; skipping", rule.id)
                continue
        self._db.commit()
        for payload in firing_payloads:
            self._record_firing(payload)
        return AlertEvaluateResult(evaluated=len(rules), fired=fired, skipped_cooldown=skipped_cooldown)

    def _record_firing(self, payload: dict[str, Any]) -> None:
        """Persist one firing row. Best-effort: a failure here never affects the
        rule's ``last_fired_at`` (already committed) or other firings."""
        try:
            self._db.add(AlertFiring(**payload))
            self._db.commit()
        except Exception:
            logger.exception("alert firing record failed for rule %s", payload.get("rule_id"))
            try:
                self._db.rollback()
            except Exception:  # noqa: BLE001 — rollback must never mask the original error
                pass

    def history(
        self,
        rule_id: int,
        *,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        limit: int = 100,
    ) -> list[AlertFiring]:
        """Most-recent-first firing timeline for one rule."""
        stmt = select(AlertFiring).where(AlertFiring.rule_id == rule_id)
        if from_dt is not None:
            stmt = stmt.where(AlertFiring.fired_at >= from_dt)
        if to_dt is not None:
            stmt = stmt.where(AlertFiring.fired_at <= to_dt)
        stmt = stmt.order_by(AlertFiring.fired_at.desc()).limit(max(1, int(limit)))
        return list(self._db.scalars(stmt))

    def list_firings(self, rule_id: int | None = None, *, limit: int = 100) -> list[AlertFiring]:
        """Most-recent-first firing timeline across all (or one) rules."""
        stmt = select(AlertFiring)
        if rule_id is not None:
            stmt = stmt.where(AlertFiring.rule_id == rule_id)
        stmt = stmt.order_by(AlertFiring.fired_at.desc()).limit(max(1, int(limit)))
        return list(self._db.scalars(stmt))

    def effectiveness(
        self,
        *,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
    ) -> list[AlertRuleEffectiveness]:
        """Read-only per-rule firing effectiveness. No notification side effects.

        ``firing_count`` is the number of ``AlertFiring`` rows within the
        optional ``[from_dt, to_dt]`` window (scoped to the requested window).

        ``last_fired_at`` / ``never_fired`` are *all-time* semantics, not
        window-scoped:

        - If ``AlertRule.last_fired_at`` is set, it is used and
          ``never_fired`` is False.
        - Otherwise the all-time latest ``AlertFiring`` (ignoring the window)
          is used as ``last_fired_at``; ``never_fired`` is True only when the
          rule has no ``AlertFiring`` row at all (in any window).

        This means a rule that fired only *outside* the requested window still
        reports ``never_fired=False`` with the all-time latest fire as
        ``last_fired_at`` and ``firing_count=0`` for the window. Deterministic
        ordering: rule id desc.
        """
        rules = list(self._db.scalars(select(AlertRule).order_by(AlertRule.id.desc())))
        if not rules:
            return []
        # Window-scoped aggregation: firing_count + latest firing within window.
        window_agg = select(
            AlertFiring.rule_id,
            func.count(AlertFiring.id),
            func.max(AlertFiring.fired_at),
        )
        if from_dt is not None:
            window_agg = window_agg.where(AlertFiring.fired_at >= from_dt)
        if to_dt is not None:
            window_agg = window_agg.where(AlertFiring.fired_at <= to_dt)
        window_agg = window_agg.group_by(AlertFiring.rule_id)
        window_stats: dict[int, tuple[int, datetime | None]] = {
            rule_id: (int(count), latest)
            for rule_id, count, latest in self._db.execute(window_agg)
        }
        # All-time aggregation: latest firing across all windows, used only to
        # resolve never_fired / last_fired_at when AlertRule.last_fired_at is
        # null (e.g. legacy rules whose last_fired_at was never populated, or
        # rules whose last_fired_at was lost). Scoped to rule ids present.
        rule_ids = [r.id for r in rules]
        alltime_latest: dict[int, datetime] = {}
        if rule_ids:
            alltime_agg = (
                select(AlertFiring.rule_id, func.max(AlertFiring.fired_at))
                .where(AlertFiring.rule_id.in_(rule_ids))
                .group_by(AlertFiring.rule_id)
            )
            alltime_latest = {
                int(rid): latest
                for rid, latest in self._db.execute(alltime_agg)
                if latest is not None
            }
        out: list[AlertRuleEffectiveness] = []
        for rule in rules:
            count, _window_latest = window_stats.get(rule.id, (0, None))
            if rule.last_fired_at is not None:
                last = rule.last_fired_at
            else:
                # Fall back to the all-time latest firing (across all windows)
                # so a rule that fired only outside the window still reports
                # never_fired=False with a real last_fired_at.
                last = alltime_latest.get(rule.id)
            out.append(AlertRuleEffectiveness(
                id=rule.id,
                name=rule.name,
                symbol=rule.symbol,
                rule_type=rule.rule_type,
                threshold=float(rule.threshold),
                severity=rule.severity,
                enabled=rule.enabled,
                cooldown_seconds=int(rule.cooldown_seconds),
                created_at=rule.created_at,
                firing_count=count,
                last_fired_at=last,
                never_fired=last is None,
            ))
        return out

    def _current_value(
        self,
        rule: AlertRule,
        quote_map: dict[str, float],
        margin_infos: list[Any] | None = None,
    ) -> float | None:
        if rule.rule_type in PRICE_RULES:
            return quote_map.get(rule.symbol)
        if rule.rule_type in INTERVAL_RULES:
            return self._interval_deviation_pct(rule, quote_map)
        if rule.rule_type in BROKER_ACCOUNT_RULES:
            if not margin_infos:
                return None
            if rule.rule_type == "margin_risk_level":
                return float(max(_margin_risk_level(mi) for mi in margin_infos))
            return float(max(_margin_call_amount(mi) for mi in margin_infos))
        if rule.rule_type == "daily_loss":
            # daily_loss keeps its pre-c566e76 contract: read the RuntimeState
            # row matching rule.symbol; a blank symbol with no blank row falls
            # back to the latest row by id. Symbol-specific missing state must
            # NOT fall back to an unrelated symbol's row.
            state = self._daily_loss_state(rule)
            return float(state.daily_pnl) if state is not None else None
        if rule.rule_type in ACCOUNT_RULES:
            # Account-wide-only: resolve the authoritative account state from
            # the latest StrategyConfig symbol, never a secondary symbol's row.
            # A manually-persisted legacy rule with a non-empty symbol is
            # still resolved from the authoritative account state (the symbol
            # is ignored for evaluation), never that secondary symbol's row.
            state = self._account_runtime_state()
            if state is None:
                return None
            if rule.rule_type == "consecutive_losses":
                return float(state.consecutive_losses)
            if rule.rule_type == "kill_switch_engaged":
                # Boolean state projected as 1.0 (engaged) / 0.0 (clear). The
                # schema forces threshold == 1.0, so only a truly engaged
                # kill switch can satisfy ``value >= threshold``.
                return 1.0 if state.kill_switch else 0.0
        return None

    def _interval_symbol(self, rule: AlertRule) -> str | None:
        """Resolve which symbol's interval a rule watches.

        A blank symbol follows whichever symbol is currently primary, so one
        rule keeps reporting drift across a primary switch. Pinning to a symbol
        that is no longer configured resolves nothing and the rule goes quiet,
        which is how the previous primary's rule stopped reporting.
        """
        if rule.symbol:
            return rule.symbol
        config = self._db.scalar(
            select(StrategyConfig).order_by(StrategyConfig.id.desc())
        )
        return config.symbol if config is not None else None

    def _interval_deviation_pct(
        self,
        rule: AlertRule,
        quote_map: dict[str, float],
    ) -> float | None:
        """Project how far price sits outside the symbol's active interval.

        A sustained trend keeps LLM confidence below the apply gate, so the
        live interval stops being refreshed and eventually drifts so far from
        price that it can never trigger. Price sitting outside the interval is
        the observable signature of that dead state. Returns 0.0 while price is
        inside the interval so a rule can only fire on real drift.
        """
        symbol = self._interval_symbol(rule)
        if symbol is None:
            return None
        price = quote_map.get(symbol)
        if price is None or price <= 0:
            return None
        config = self._db.scalar(
            select(StrategyConfig)
            .where(StrategyConfig.symbol == symbol)
            .order_by(StrategyConfig.id.desc())
        )
        if config is None:
            return None
        try:
            buy_low = float(config.buy_low)
            sell_high = float(config.sell_high)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(buy_low) or not math.isfinite(sell_high):
            return None
        if buy_low <= 0 or sell_high <= 0 or sell_high <= buy_low:
            return None
        if price > sell_high:
            return (price - sell_high) / sell_high * 100
        if price < buy_low:
            return (buy_low - price) / buy_low * 100
        return 0.0

    def _daily_loss_state(self, rule: AlertRule) -> RuntimeState | None:
        """Resolve the ``RuntimeState`` row a daily_loss rule reads from.

        Preserves the exact pre-c566e76 contract: query RuntimeState matching
        ``rule.symbol`` (ordered by id desc); when ``rule.symbol`` is blank
        and no blank row exists, fall back to the latest row by id regardless
        of symbol. A symbol-specific rule with no matching state row returns
        ``None`` (data unavailable) — it never falls back to an unrelated
        symbol's row.
        """
        state = self._db.scalar(
            select(RuntimeState)
            .where(RuntimeState.symbol == rule.symbol)
            .order_by(RuntimeState.id.desc())
        )
        if state is None and not rule.symbol:
            state = self._db.scalar(select(RuntimeState).order_by(RuntimeState.id.desc()))
        return state

    def _account_runtime_state(self) -> RuntimeState | None:
        """Resolve the authoritative account ``RuntimeState`` (read-only).

        Mirrors the read-only portion of
        ``StrategyService.get_primary_runtime_state`` without any of its
        mutations (no row creation, no legacy-row symbol reassignment):

        1. Query the latest ``StrategyConfig`` (by id desc) and normalize its
           ``symbol``.
        2. If a primary symbol is configured, read that symbol's
           ``RuntimeState`` row.
        3. If no primary-symbol row is available, fall back to the legacy
           ``RuntimeState`` row with ``symbol == ""``.
        4. If neither exists, return ``None`` (no data) — never fall back to
           an arbitrary secondary symbol's row.
        """
        config = self._db.scalar(select(StrategyConfig).order_by(StrategyConfig.id.desc()))
        primary_symbol = (config.symbol or "").strip().upper() if config is not None else ""
        if primary_symbol:
            named = self._db.scalar(
                select(RuntimeState).where(RuntimeState.symbol == primary_symbol)
            )
            if named is not None:
                return named
            # No primary-symbol row — fall back to the legacy empty-symbol row.
            legacy = self._db.scalar(
                select(RuntimeState).where(RuntimeState.symbol == "")
            )
            if legacy is not None:
                return legacy
            return None
        # No primary symbol configured: use the legacy empty-symbol row only.
        return self._db.scalar(select(RuntimeState).where(RuntimeState.symbol == ""))

    @staticmethod
    def _to_out(rule: AlertRule) -> AlertRuleOut:
        return AlertRuleOut.model_validate(rule)


def _eligible(rule: AlertRule, now: datetime) -> bool:
    if rule.last_fired_at is None:
        return True
    last = rule.last_fired_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() >= max(0, int(rule.cooldown_seconds))


def _check(rule: AlertRule, value: float, symbol: str | None = None) -> tuple[bool, str]:
    display_symbol = rule.symbol if symbol is None else symbol
    if rule.rule_type == "price_above":
        triggered = value >= rule.threshold
        return triggered, f"{rule.symbol} 现价 {value:.2f} ≥ {rule.threshold:.2f}"
    if rule.rule_type == "price_below":
        triggered = value <= rule.threshold
        return triggered, f"{rule.symbol} 现价 {value:.2f} ≤ {rule.threshold:.2f}"
    if rule.rule_type == "daily_loss":
        triggered = value <= rule.threshold
        return triggered, f"{rule.symbol} 日内盈亏 {value:.2f} ≤ 阈值 {rule.threshold:.2f}"
    if rule.rule_type == "consecutive_losses":
        # threshold is validated as a positive integer at the schema layer;
        # compare as ints to avoid float drift on a count. Account-wide rule —
        # the message is branded with the rule name, not a per-rule symbol.
        triggered = int(value) >= int(rule.threshold)
        return triggered, f"账户连续亏损 {int(value)} ≥ 阈值 {int(rule.threshold)}"
    if rule.rule_type == "kill_switch_engaged":
        # threshold is forced to 1.0 at the schema layer; value is 1.0 when
        # the kill switch is engaged and 0.0 otherwise. Notification-only —
        # evaluation never mutates RiskController or RuntimeState. Account-wide
        # rule — branded with the rule name, not a per-rule symbol.
        triggered = value >= rule.threshold
        status = "已触发" if triggered else "未触发"
        return triggered, f"账户熔断开关 {status}"
    if rule.rule_type == "interval_stale":
        # value is the deviation percentage of price outside the active
        # interval (0.0 while price is inside), so a positive threshold can
        # never fire on a healthy interval.
        triggered = value >= rule.threshold
        return triggered, (
            f"{display_symbol} 现价已偏离活动区间 {value:.2f}% ≥ 阈值 "
            f"{rule.threshold:.2f}%，区间可能已失效需人工复核"
        )
    if rule.rule_type == "margin_risk_level":
        triggered = int(value) >= int(rule.threshold)
        label = _RISK_LEVEL_LABELS.get(int(value), "未知")
        return triggered, (
            f"账户保证金风控等级 {int(value)}（{label}） ≥ 阈值 {int(rule.threshold)}，"
            f"融资账户存在强平风险需人工复核"
        )
    if rule.rule_type == "margin_call":
        triggered = value >= rule.threshold
        return triggered, (
            f"账户追缴保证金 {value:.2f} ≥ 阈值 {rule.threshold:.2f}，"
            f"需尽快补足保证金或减仓"
        )
    return False, ""


def _fetch_margin_infos(account_provider: Any) -> list[Any] | None:
    if account_provider is None:
        return None
    try:
        account = account_provider.get_account()
    except Exception:
        # Same rationale as the quote fetch: a silent broker outage would look
        # identical to a healthy margin account, which is the exact failure this
        # alert exists to catch.
        logger.warning("alert-rule margin snapshot fetch failed", exc_info=True)
        return None
    margin_infos = getattr(account, "margin_infos", None)
    return list(margin_infos) if margin_infos else None


def _margin_risk_level(margin_info: Any) -> int:
    try:
        return int(getattr(margin_info, "risk_level", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _margin_call_amount(margin_info: Any) -> float:
    try:
        return float(getattr(margin_info, "margin_call", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


_RISK_LEVEL_LABELS = {0: "安全", 1: "中风险", 2: "预警", 3: "危险"}


def _fetch_quotes(quote_provider: Any, symbols: list[str]) -> dict[str, float]:
    if not symbols or quote_provider is None:
        return {}
    try:
        quotes = quote_provider.get_quotes(symbols)
    except Exception:
        # A persistent broker outage is indistinguishable from "no rules
        # triggered" unless we log it — surface it so operators notice price
        # alerts stopped working.
        logger.warning("alert-rule quote fetch failed for %d symbols", len(symbols), exc_info=True)
        return {}
    out: dict[str, float] = {}
    for q in quotes:
        symbol = getattr(q, "symbol", None)
        last_price = getattr(q, "last_price", 0) or 0
        if symbol and last_price > 0:
            out[symbol] = float(last_price)
    return out
