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
from dataclasses import dataclass
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

# Strict ASCII target-date shape: exactly four digits, dash, two digits, dash,
# two digits — full match only. Rejects single-digit month/day, Unicode
# digits, and any leading/trailing content.
_TARGET_DATE_RE = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _validate_target_date(target_date: str | None) -> str:
    """Validate and return the resolved target date string.

    ``None`` defaults to the current UTC date (preserving scheduled/manual
    behavior). A non-None value must be an ASCII ``YYYY-MM-DD`` string with a
    real calendar date: full-match shape, no Unicode digits, no leading/
    trailing content, and a date that actually exists (e.g. rejects
    ``2026-06-31``). Raises ``ValueError`` otherwise so API callers can map it
    to HTTP 400.
    """
    if target_date is None:
        return datetime.now(timezone.utc).date().isoformat()
    # strptime accepts some non-ASCII digit forms on some platforms and is
    # lenient about trailing content in certain modes; enforce the strict
    # ASCII shape first, then verify calendar validity.
    if not _TARGET_DATE_RE.fullmatch(target_date):
        raise ValueError("target_date must be YYYY-MM-DD")
    # Calendar validity: rejects impossible dates like 2026-06-31 and
    # 2026-02-30. strptime with %Y-%m-%d is strict about real dates.
    datetime.strptime(target_date, "%Y-%m-%d")
    return target_date


@dataclass(frozen=True)
class ReportScheduleStatus:
    """Safe, serializable operational snapshot of the scheduled-report state.

    All monotonic timestamps are deliberately excluded; only derived, clamped
    durations are exposed. ``state_scope``/``resets_on_restart`` make the
    process-local, non-persistent nature of the throttle explicit.
    """

    enabled: bool
    configured_symbol: str
    effective_symbol: str
    interval_hours: int
    has_process_send_history: bool
    last_sent_age_seconds: float | None
    next_eligible_in_seconds: float | None
    eligible_now: bool
    state_scope: str = "process"
    resets_on_restart: bool = True


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
        preserving the original scheduled-report behavior. When provided it is
        validated through ``_validate_target_date`` (strict ASCII
        ``YYYY-MM-DD`` + real calendar date); an invalid value raises
        ``ValueError`` so API callers can map it to HTTP 400 (the underlying
        report build itself is still guarded and never raises).

        Never raises for report-build failures: if the report cannot be built,
        returns a short 'no data' message so the notification still sends
        something useful.
        """
        title = f"交易日报 · {symbol}"
        target = _validate_target_date(target_date)
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

        Only an explicit *symbol_override* is market-validated; a
        configured/fallback symbol is passed straight to ``build_summary`` so
        that an invalid legacy configured symbol produces the same fallback
        title/content as manual/scheduled send rather than a preview 400. This
        keeps preview parity with the actual send path.

        This method never calls the notifier, never writes audit rows, and never
        mutates the process-local ``_LAST_SENT`` throttle. Raises
        ``ValueError`` when no effective symbol can be resolved, when an
        override is invalid, or when the target date is invalid.
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
            # Deliberately do NOT market-validate the configured/fallback
            # symbol here: pass it straight to build_summary so an invalid
            # legacy configured symbol yields the same fallback title/content
            # as manual/scheduled send (parity), not a preview 400.

        resolved_target = _validate_target_date(target_date)

        title, content = self.build_summary(symbol, target_date=resolved_target)
        return symbol, resolved_target, title, content

    def status(self) -> ReportScheduleStatus:
        """Return a safe operational snapshot of the scheduled-report state.

        Read-only: does not call the notifier, mutate ``_LAST_SENT``, write
        DB/audit rows, or change config. Reuses the same effective-symbol and
        interval resolution as ``maybe_send`` so status cannot drift from the
        actual send decision.

        ``last_sent_age_seconds`` is the elapsed time since the last send,
        clamped to ``max(0, raw_elapsed)`` so a clock rollback never reports a
        negative age. ``next_eligible_in_seconds`` and ``eligible_now`` use the
        *raw* elapsed exactly like ``maybe_send``'s throttle gate
        (``raw_elapsed < window``), so with a clock rollback the reported
        remaining wait may exceed the configured interval and ``eligible_now``
        stays False until the rollback is resolved — mirroring the point at
        which ``maybe_send`` would actually dispatch.
        """
        cfg = self._db.query(StrategyConfig).order_by(
            StrategyConfig.id.desc()
        ).first()
        enabled = bool(getattr(cfg, "report_schedule_enabled", False)) if cfg else False
        configured_symbol = (
            str(getattr(cfg, "report_schedule_symbol", "") or "").strip().upper()
            if cfg
            else ""
        )
        effective_symbol = self.resolve_effective_symbol(cfg) if cfg else ""
        interval_hours = max(
            1, int(getattr(cfg, "report_schedule_interval_hours", 24) or 24)
        )

        now = self._clock()
        last = self._state.get(effective_symbol) if effective_symbol else None
        has_history = last is not None

        window_seconds = interval_hours * 3600
        if has_history:
            raw_elapsed = now - last
            # Exposed age is clamped to zero so rollback never shows a negative
            # age to operators.
            last_sent_age_seconds: float | None = max(0.0, raw_elapsed)
            # Remaining wait and eligibility use the RAW elapsed exactly like
            # maybe_send's gate (raw_elapsed < window). With a rollback
            # (negative raw_elapsed) the remaining wait exceeds the window and
            # eligible_now stays False, matching when maybe_send would dispatch.
            remaining = window_seconds - raw_elapsed
            next_eligible_in_seconds: float | None = max(0.0, remaining)
        else:
            last_sent_age_seconds = None
            next_eligible_in_seconds = None

        # eligible_now mirrors maybe_send's gate: enabled + effective symbol +
        # (no prior send OR raw_elapsed >= window). Uses raw elapsed so a
        # rolled-back clock cannot make a recently-sent report appear eligible.
        throttle_elapsed = (last is None) or ((now - last) >= window_seconds)
        eligible_now = bool(enabled and effective_symbol and throttle_elapsed)

        return ReportScheduleStatus(
            enabled=enabled,
            configured_symbol=configured_symbol,
            effective_symbol=effective_symbol,
            interval_hours=interval_hours,
            has_process_send_history=bool(has_history),
            last_sent_age_seconds=last_sent_age_seconds,
            next_eligible_in_seconds=next_eligible_in_seconds,
            eligible_now=eligible_now,
        )

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
