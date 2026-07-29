"""Universe selection explainer — human-readable "why was X selected/rejected".

Read-only join over ``universe_selection_runs`` and
``universe_selection_candidates`` that surfaces, for any symbol or run, the
factor score breakdown, hard-filter pass/fail list, and a peer comparison.
Nothing here mutates state; it is a research/audit surface only.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models import UniverseSelectionCandidate, UniverseSelectionRun

logger = logging.getLogger("auto_trade.universe_explainer")

# Numeric metric keys surfaced in the score_breakdown. We pull the union of
# whatever the selection algorithm wrote into metrics_json (factor→score),
# but also lift the top-level ``score`` and ``rank`` for convenience.
_METRIC_NUMERIC_TYPES = (int, float)


def _parse_json_object(raw: str | None, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not raw:
        return dict(default or {})
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return dict(default or {})
    return parsed if isinstance(parsed, dict) else dict(default or {})


def _parse_json_list(raw: str | None) -> list[Any]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


class UniverseExplainerService:
    """Explain universe-selection outcomes for a symbol or an entire run."""

    # The exclusion-reason taxonomy is owned by the selection domain; we only
    # need to bucket reasons into "passed" (no reason) vs "failed" (reasons
    # present). Both lists are informational — we never reject an unknown
    # reason, we simply surface it verbatim.
    KNOWN_HARD_FILTER_NAMES: tuple[str, ...] = (
        "INSUFFICIENT_HISTORY",
        "ILLIQUID",
        "WIDE_SPREAD",
        "SECTOR_QUOTA",
        "DUPLICATE_MEMBERSHIP",
        "BLOCKED_SYMBOL",
        "STALE_BARS",
        "VOLATILITY_OUT_OF_RANGE",
        "RISK_GROUP_SATURATED",
    )

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Symbol-level explanation
    # ------------------------------------------------------------------
    def explain_selection(self, symbol: str) -> dict[str, Any]:
        """Explain why ``symbol`` was selected or rejected in its latest run."""
        normalized = symbol.strip().upper()
        empty = self._empty_symbol_response(normalized)
        if not normalized:
            return empty

        run = self._latest_run()
        if run is None:
            return empty

        candidate = self._candidate_for_run(run.id, normalized)
        if candidate is None:
            # Symbol was never considered in this run.
            return {
                **empty,
                "run_id": run.id,
                "as_of_date": run.as_of_date.isoformat(),
            }

        exclusion_reasons = self._exclusion_reasons(candidate)
        metrics = _parse_json_object(candidate.metrics_json)
        score_breakdown = self._score_breakdown(metrics, candidate)

        return {
            "symbol": normalized,
            "run_id": run.id,
            "as_of_date": run.as_of_date.isoformat(),
            "selected": bool(candidate.selected),
            "rank": candidate.rank,
            "score": float(candidate.score),
            "score_breakdown": score_breakdown,
            "hard_filters_passed": self._hard_filters_passed(metrics, exclusion_reasons),
            "hard_filters_failed": exclusion_reasons,
            "peer_comparison": self._peer_comparison(run.id, normalized),
        }

    # ------------------------------------------------------------------
    # Run-level explanation
    # ------------------------------------------------------------------
    def explain_run(self, run_id: int | None = None) -> dict[str, Any]:
        """Summarize a selection run: coverage, top selected, top rejected."""
        run: UniverseSelectionRun | None
        if run_id is not None:
            run = self._db.get(UniverseSelectionRun, run_id)
        else:
            run = self._latest_run()
        if run is None:
            return self._empty_run_response(run_id)

        candidates = (
            self._db.query(UniverseSelectionCandidate)
            .filter(UniverseSelectionCandidate.run_id == run.id)
            .all()
        )
        selected = [c for c in candidates if c.selected]
        rejected = [c for c in candidates if not c.selected]

        return {
            "run_id": run.id,
            "as_of_date": run.as_of_date.isoformat(),
            "status": run.status,
            "total_candidates": len(candidates),
            "selected_count": len(selected),
            "coverage_ratio": float(run.coverage_ratio),
            "top_selected": self._top_candidates(selected, 10),
            "top_rejected": self._top_rejected(rejected, 10),
        }

    # ------------------------------------------------------------------
    # Helpers — queries
    # ------------------------------------------------------------------
    def _latest_run(self) -> UniverseSelectionRun | None:
        return (
            self._db.query(UniverseSelectionRun)
            .order_by(
                UniverseSelectionRun.created_at.desc(),
                UniverseSelectionRun.id.desc(),
            )
            .first()
        )

    def _candidate_for_run(
        self, run_id: int, symbol: str
    ) -> UniverseSelectionCandidate | None:
        return (
            self._db.query(UniverseSelectionCandidate)
            .filter(
                UniverseSelectionCandidate.run_id == run_id,
                UniverseSelectionCandidate.symbol == symbol,
            )
            .first()
        )

    def _peer_comparison(self, run_id: int, focus_symbol: str) -> list[dict[str, Any]]:
        peers = (
            self._db.query(UniverseSelectionCandidate)
            .filter(UniverseSelectionCandidate.run_id == run_id)
            .order_by(
                UniverseSelectionCandidate.selected.desc(),
                UniverseSelectionCandidate.score.desc(),
                UniverseSelectionCandidate.rank.asc(),
            )
            .limit(5)
            .all()
        )
        return [self._candidate_summary(p, focus_symbol) for p in peers]

    # ------------------------------------------------------------------
    # Helpers — shaping
    # ------------------------------------------------------------------
    def _score_breakdown(
        self,
        metrics: dict[str, Any],
        candidate: UniverseSelectionCandidate,
    ) -> dict[str, Any]:
        """Factor→score view of ``metrics_json`` plus the headline score/rank."""
        breakdown: dict[str, Any] = {
            "headline_score": float(candidate.score),
            "rank": candidate.rank,
        }
        for key, value in metrics.items():
            if isinstance(value, _METRIC_NUMERIC_TYPES):
                breakdown[key] = float(value)
            elif isinstance(value, dict):
                # Nested rotation/factor dicts: flatten one level so the UI can
                # render each factor on its own row.
                for sub_key, sub_value in value.items():
                    if isinstance(sub_value, _METRIC_NUMERIC_TYPES):
                        breakdown[f"{key}.{sub_key}"] = float(sub_value)
            elif isinstance(value, str):
                try:
                    breakdown[key] = float(value)
                except ValueError:
                    # Non-numeric metadata (sector, alias, etc.) is intentionally
                    # dropped — the breakdown is a numeric factor view only.
                    continue
        return breakdown

    def _hard_filters_passed(
        self,
        metrics: dict[str, Any],
        exclusion_reasons: list[str],
    ) -> list[str]:
        """Return the known hard filters that did NOT fire for this candidate.

        ``metrics_json`` carries the boolean-style gates the selector actually
        evaluated; exclusion_reasons carries the failures. We treat any known
        filter name absent from exclusion_reasons as "passed". If metrics
        carries explicit boolean filter flags, we honor those instead.
        """
        failed = {reason.strip().upper() for reason in exclusion_reasons if reason}
        explicit = {
            key: value
            for key, value in metrics.items()
            if isinstance(value, bool) and key.upper().startswith(("IS_", "HAS_", "PASSES_"))
        }
        if explicit:
            return sorted(
                key for key, value in explicit.items() if value is True
            )
        return sorted(
            name for name in self.KNOWN_HARD_FILTER_NAMES if name not in failed
        )

    def _exclusion_reasons(
        self, candidate: UniverseSelectionCandidate
    ) -> list[str]:
        raw = _parse_json_list(candidate.exclusion_reasons_json)
        reasons: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                reasons.append(item.strip())
        return reasons

    def _candidate_summary(
        self, candidate: UniverseSelectionCandidate, focus_symbol: str
    ) -> dict[str, Any]:
        return {
            "symbol": candidate.symbol,
            "selected": bool(candidate.selected),
            "rank": candidate.rank,
            "score": float(candidate.score),
            "is_focus": candidate.symbol == focus_symbol,
            "exclusion_reasons": self._exclusion_reasons(candidate),
        }

    def _top_candidates(
        self, candidates: list[UniverseSelectionCandidate], limit: int
    ) -> list[dict[str, Any]]:
        ordered = sorted(
            candidates,
            key=lambda c: (
                c.rank if c.rank is not None else float("inf"),
                -float(c.score),
                c.symbol,
            ),
        )
        return [self._candidate_summary(c, "") for c in ordered[:limit]]

    def _top_rejected(
        self, candidates: list[UniverseSelectionCandidate], limit: int
    ) -> list[dict[str, Any]]:
        # Rejected candidates have no rank; rank by score desc so the "closest
        # miss" surfaces first, then by symbol for determinism.
        ordered = sorted(
            candidates,
            key=lambda c: (-float(c.score), c.symbol),
        )
        return [self._candidate_summary(c, "") for c in ordered[:limit]]

    # ------------------------------------------------------------------
    # Empty-state responses
    # ------------------------------------------------------------------
    @staticmethod
    def _empty_symbol_response(symbol: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "run_id": None,
            "as_of_date": None,
            "selected": False,
            "rank": None,
            "score": 0.0,
            "score_breakdown": {},
            "hard_filters_passed": [],
            "hard_filters_failed": [],
            "peer_comparison": [],
        }

    @staticmethod
    def _empty_run_response(run_id: int | None) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "as_of_date": None,
            "status": "NOT_FOUND",
            "total_candidates": 0,
            "selected_count": 0,
            "coverage_ratio": 0.0,
            "top_selected": [],
            "top_rejected": [],
        }
