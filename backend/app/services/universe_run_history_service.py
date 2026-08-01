"""DB-only universe selection run history — read-only, no live runtime.

The smallest possible query service for the run-history endpoint: it takes the
request ``Session`` and queries ``UniverseSelectionRun`` rows ONLY. It never
calls ``get_runner``, the broker, settings-driven selection construction,
catalog evaluation, refresh, quote retrieval, shadow synchronization, or any
write.

This deliberately does NOT reuse ``UniverseSelectionService`` (which constructs
the broker/catalog/selection config) so the history path cannot accidentally
trigger selection or quote fetches. ``UniverseSelectionService.list_runs`` is
retained for callers that already hold a fully-constructed service, but the
HTTP history endpoint uses this DB-only service.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import UniverseSelectionRun
from app.schemas import UniverseSelectionRunPage, UniverseSelectionRunSummary

__all__ = ["UniverseRunHistoryService"]


class UniverseRunHistoryService:
    """Read-only bounded paginated universe-selection run history (DB-only)."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def list_runs(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> UniverseSelectionRunPage:
        """Return a bounded paginated history page querying stored rows only.

        Ordering is stable newest-first by the authoritative ``as_of_date``
        then ``created_at`` then ``id`` (mirroring ``latest_run``), so
        identical timestamps paginate deterministically without duplicates or
        omissions. ``from_date`` / ``to_date`` form an inclusive range over
        ``as_of_date``. Raises ``ValueError`` on an inverted range.
        ``page_size`` is capped at 100.
        """
        bounded_page = max(1, int(page))
        bounded_size = max(1, min(int(page_size), 100))
        if (
            from_date is not None
            and to_date is not None
            and from_date > to_date
        ):
            raise ValueError("from_date must be on or before to_date")

        base = select(UniverseSelectionRun)
        if from_date is not None:
            base = base.where(UniverseSelectionRun.as_of_date >= from_date)
        if to_date is not None:
            base = base.where(UniverseSelectionRun.as_of_date <= to_date)

        total = int(
            self._db.scalar(
                select(func.count())
                .select_from(base.subquery())
            )
            or 0
        )

        rows = list(
            self._db.scalars(
                base.order_by(
                    UniverseSelectionRun.as_of_date.desc(),
                    UniverseSelectionRun.created_at.desc(),
                    UniverseSelectionRun.id.desc(),
                )
                .offset((bounded_page - 1) * bounded_size)
                .limit(bounded_size)
            )
        )

        filters: dict[str, object] = {
            "page": bounded_page,
            "page_size": bounded_size,
        }
        if from_date is not None:
            filters["from_date"] = from_date.isoformat()
        if to_date is not None:
            filters["to_date"] = to_date.isoformat()

        return UniverseSelectionRunPage(
            items=[
                UniverseSelectionRunSummary.model_validate(run) for run in rows
            ],
            total=total,
            page=bounded_page,
            page_size=bounded_size,
            filters=filters,
        )
