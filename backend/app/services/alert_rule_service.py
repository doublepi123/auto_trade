"""Conditional alert rules — user-defined thresholds evaluated by a cron.

Broker/notifier-agnostic: ``evaluate`` reads live quotes (price rules) and the
active ``RuntimeState.daily_pnl`` (daily_loss rules) via an injected runner and
dispatches through its notifier, respecting a per-rule cooldown. Never touches
the live order path — it only reads and notifies.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AlertFiring, AlertRule, RuntimeState
from app.schemas import (
    AlertEvaluateResult,
    AlertRuleCreate,
    AlertRuleEffectiveness,
    AlertRuleOut,
)

logger = logging.getLogger(__name__)

PRICE_RULES = {"price_above", "price_below"}
# Rule types that read ``RuntimeState`` instead of live broker quotes. They
# never trigger a quote fetch — the cron can evaluate them without a broker.
STATE_RULES = {"daily_loss", "consecutive_losses"}


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
        symbols = sorted({r.symbol for r in rules if r.rule_type in PRICE_RULES and r.symbol})
        quote_map = _fetch_quotes(quote_provider, symbols)

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
                value = self._current_value(rule, quote_map)
                if value is None:
                    continue  # data unavailable (no quote / no state) — skip silently
                triggered, message = _check(rule, value)
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
                            "symbol": str(rule.symbol or ""),
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

    def _current_value(self, rule: AlertRule, quote_map: dict[str, float]) -> float | None:
        if rule.rule_type in PRICE_RULES:
            return quote_map.get(rule.symbol)
        if rule.rule_type in STATE_RULES:
            state = self._runtime_state_for_rule(rule)
            if state is None:
                return None
            if rule.rule_type == "daily_loss":
                return float(state.daily_pnl)
            if rule.rule_type == "consecutive_losses":
                return float(state.consecutive_losses)
        return None

    def _runtime_state_for_rule(self, rule: AlertRule) -> RuntimeState | None:
        """Resolve the ``RuntimeState`` row a state-backed rule reads from.

        Symbol-specific rules read only that symbol's row — a missing row
        returns ``None`` (data unavailable) rather than falling back to an
        unrelated symbol's state. Symbol-empty (account-wide) rules use the
        most recently updated row regardless of symbol.
        """
        if rule.symbol:
            return self._db.scalar(
                select(RuntimeState).where(RuntimeState.symbol == rule.symbol)
            )
        return self._db.scalar(select(RuntimeState).order_by(RuntimeState.id.desc()))

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


def _check(rule: AlertRule, value: float) -> tuple[bool, str]:
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
        # compare as ints to avoid float drift on a count.
        triggered = int(value) >= int(rule.threshold)
        return triggered, f"{rule.symbol} 连续亏损 {int(value)} ≥ 阈值 {int(rule.threshold)}"
    return False, ""


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
