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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AlertRule, RuntimeState
from app.schemas import AlertEvaluateResult, AlertRuleCreate, AlertRuleOut

logger = logging.getLogger(__name__)

PRICE_RULES = {"price_above", "price_below"}


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
        for rule in rules:
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
        self._db.commit()
        return AlertEvaluateResult(evaluated=len(rules), fired=fired, skipped_cooldown=skipped_cooldown)

    def _current_value(self, rule: AlertRule, quote_map: dict[str, float]) -> float | None:
        if rule.rule_type in PRICE_RULES:
            return quote_map.get(rule.symbol)
        if rule.rule_type == "daily_loss":
            state = self._db.scalar(
                select(RuntimeState)
                .where(RuntimeState.symbol == rule.symbol)
                .order_by(RuntimeState.id.desc())
            )
            if state is None and rule.symbol:
                # fall back to the latest row regardless of symbol
                state = self._db.scalar(select(RuntimeState).order_by(RuntimeState.id.desc()))
            return float(state.daily_pnl) if state is not None else None
        return None

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
    return False, ""


def _fetch_quotes(quote_provider: Any, symbols: list[str]) -> dict[str, float]:
    if not symbols or quote_provider is None:
        return {}
    try:
        quotes = quote_provider.get_quotes(symbols)
    except Exception:
        return {}
    out: dict[str, float] = {}
    for q in quotes:
        symbol = getattr(q, "symbol", None)
        last_price = getattr(q, "last_price", 0) or 0
        if symbol and last_price > 0:
            out[symbol] = float(last_price)
    return out
