"""Scheduled performance reports.

Builds a human-readable daily report (reusing ``ReportService``) and sends it
through the runner's ``MultiChannelNotifier``. Driven by StrategyConfig flags so
it can be toggled from the UI; an in-memory per-symbol monotonic throttle keeps
it from spamming. ``maybe_send`` is broker/notifier-agnostic and injectable for
tests.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from sqlalchemy.orm import Session

from app.models import StrategyConfig
from app.services.report_service import ReportService

logger = logging.getLogger(__name__)

# symbol -> monotonic timestamp of last successful send. Module-level so the
# background cron shares state across ticks within a process lifetime.
_LAST_SENT: dict[str, float] = {}

# US/HK market suffixes only, matching ReportService._normalize_symbol. Kept
# local so preview/normalization does not depend on importing the report
# service's private regex.
_PREVIEW_SYMBOL_RE = re.compile(r"^[A-Z0-9\-]{1,12}\.(US|HK)$")


def normalize_preview_symbol(symbol: str) -> str:
    """Normalize and validate a report symbol for preview/manual/scheduled use.

    Accepts the same CODE.MARKET shape as ``ReportService._normalize_symbol``
    (US/HK only) but is safe to call from the API layer without constructing a
    ``ReportService``. Raises ``ValueError`` with a stable message on invalid
    input; the caller is expected to map that to an HTTP 400.
    """
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        raise ValueError("symbol is required")
    if not _PREVIEW_SYMBOL_RE.fullmatch(normalized):
        raise ValueError("symbol market must be US or HK")
    return normalized


class _NotifierLike(Protocol):
    def send(self, title: str, content: str, severity: str = "INFO") -> bool: ...


class _RunnerLike(Protocol):
    notifier: Any


class ReportScheduleService:
    def __init__(
        self,
        db: Session,
        *,
        clock: Callable[[], float] = time.monotonic,
        state: dict[str, float] | None = None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._state = state if state is not None else _LAST_SENT

    @staticmethod
    def resolve_effective_symbol(cfg: StrategyConfig | None) -> str:
        """Return the effective report symbol for a config row.

        Mirrors the precedence used by ``maybe_send`` and the manual run
        endpoint: ``report_schedule_symbol`` falls back to the active strategy
        ``symbol``. Returns an empty string when no effective symbol exists so
        callers can reject uniformly. The result is already upper-cased and
        stripped but NOT market-validated; callers that need validation should
        pass it through ``normalize_preview_symbol``.
        """
        if cfg is None:
            return ""
        return (
            getattr(cfg, "report_schedule_symbol", "") or cfg.symbol or ""
        ).strip().upper()

    def build_summary(
        self,
        symbol: str,
        *,
        target_date: str | None = None,
    ) -> tuple[str, str]:
        """Return (title, content) for a daily report on *symbol*.

        When *target_date* is omitted or ``None`` the current UTC date is used,
        preserving the original scheduled-report behavior. When provided it must
        be a ``YYYY-MM-DD`` string; an invalid format raises ``ValueError`` so
        API callers can map it to HTTP 400 (the underlying report build itself
        is still guarded and never raises).

        Never raises for report-build failures: if the report cannot be built,
        returns a short 'no data' message so the notification still sends
        something useful.
        """
        title = f"交易日报 · {symbol}"
        if target_date is None:
            target = datetime.now(timezone.utc).date().isoformat()
        else:
            # Validate the shape explicitly so preview callers get a clear
            # error rather than a downstream strptime traceback.
            datetime.strptime(target_date, "%Y-%m-%d")
            target = target_date
        try:
            report = ReportService(self._db).get_daily_report(symbol, target)
        except Exception:
            logger.exception("scheduled report build failed for %s", symbol)
            return (title, f"{symbol} {target}：报告生成失败，请查看日志。")
        quality = report.statistics_quality
        if quality.status in {"UNRESOLVED", "STALE_EXCLUSION"}:
            return (
                title,
                (
                    f"{symbol} {target}：统计未完成，已省略 "
                    f"{quality.omitted_day_count} 个标的交易日；发现 "
                    f"{quality.unresolved_issue_count} 个账本问题，请先复核。"
                ),
            )
        m = report.metrics
        if m.total_trades == 0:
            return (title, f"{symbol} {target}：今日暂无成交。")
        return (title, "\n".join([
            f"标的：{symbol}",
            f"周期：{report.start_date} ~ {report.end_date}",
            f"总盈亏：{m.total_pnl:.2f}",
            f"交易：{m.total_trades} 笔（胜 {m.win_count} / 负 {m.loss_count}，胜率 {m.win_rate:.1f}%）",
            f"盈亏比：{m.profit_loss_ratio:.2f}",
            f"最大回撤：{m.max_drawdown:.2f}",
            f"LLM 建议 {m.llm_suggestions_count}，采纳 {m.llm_applied_count}（采纳率 {m.llm_apply_rate:.1f}%）",
        ]))

    def preview(
        self,
        *,
        symbol_override: str | None = None,
        target_date: str | None = None,
    ) -> tuple[str, str, str, str]:
        """Build a preview of the scheduled report without any side effects.

        Returns ``(effective_symbol, target_date, title, content)``. Resolves
        the effective symbol the same way ``maybe_send`` does
        (``report_schedule_symbol`` -> active strategy symbol) unless
        *symbol_override* is supplied, in which case it is normalized/validated
        and used directly. *target_date* defaults to UTC today.

        This method never calls the notifier, never writes audit rows, and never
        mutates the process-local ``_LAST_SENT`` throttle. Raises
        ``ValueError`` when no effective symbol can be resolved or when an
        override/date is invalid.
        """
        if symbol_override is not None and symbol_override.strip():
            symbol = normalize_preview_symbol(symbol_override)
        else:
            cfg = self._db.query(StrategyConfig).order_by(
                StrategyConfig.id.desc()
            ).first()
            symbol = self.resolve_effective_symbol(cfg)
            if not symbol:
                raise ValueError("no effective report symbol configured")
            # Validate the configured/fallback symbol too so preview cannot
            # silently diverge from what the report service would accept.
            symbol = normalize_preview_symbol(symbol)

        if target_date is None:
            resolved_target = datetime.now(timezone.utc).date().isoformat()
        else:
            datetime.strptime(target_date, "%Y-%m-%d")
            resolved_target = target_date

        title, content = self.build_summary(symbol, target_date=resolved_target)
        return symbol, resolved_target, title, content

    def maybe_send(self, runner: _RunnerLike) -> bool:
        """Send a scheduled report if enabled and the throttle window elapsed.

        Returns True iff a notification was actually dispatched.
        """
        cfg = self._db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
        if cfg is None or not bool(getattr(cfg, "report_schedule_enabled", False)):
            return False
        symbol = self.resolve_effective_symbol(cfg)
        if not symbol:
            return False
        interval_hours = max(1, int(getattr(cfg, "report_schedule_interval_hours", 24) or 24))

        now = self._clock()
        last = self._state.get(symbol)
        if last is not None and (now - last) < interval_hours * 3600:
            return False

        title, content = self.build_summary(symbol)
        notifier = getattr(runner, "notifier", None)
        if notifier is None:
            return False
        try:
            sent = bool(notifier.send(title, content, severity="INFO"))
        except Exception:
            logger.exception("scheduled report notification failed for %s", symbol)
            return False
        if sent:
            self._state[symbol] = now
        return sent
