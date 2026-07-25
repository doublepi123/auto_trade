from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.domain.universe_selection.catalog import IndexCandidate


_DATA_PATH = (
    Path(__file__).with_name("data")
    / "index_membership_history.json"
)


@dataclass(frozen=True)
class MembershipInterval:
    start: date
    end: date | None

    def contains(self, value: date) -> bool:
        return (
            self.start <= value
            and (self.end is None or value < self.end)
        )


@dataclass(frozen=True)
class MembershipHistoryCoverage:
    catalog_size: int
    authoritative_symbols: int
    snapshot_only_symbols: tuple[str, ...]
    missing_symbols: tuple[str, ...]

    @property
    def authoritative_ratio(self) -> float:
        if self.catalog_size == 0:
            return 0.0
        return self.authoritative_symbols / self.catalog_size

    def to_dict(self) -> dict[str, object]:
        return {
            "catalog_size": self.catalog_size,
            "authoritative_symbols": self.authoritative_symbols,
            "authoritative_ratio": self.authoritative_ratio,
            "snapshot_only_symbols": list(
                self.snapshot_only_symbols
            ),
            "missing_symbols": list(self.missing_symbols),
        }


@dataclass(frozen=True)
class IndexMembershipHistory:
    source_version: str
    effective_start_date: date
    catalog_snapshot_date: date
    sources: tuple[Mapping[str, str], ...]
    intervals: Mapping[
        str,
        Mapping[str, tuple[MembershipInterval, ...]],
    ]
    snapshot_overrides: Mapping[
        str,
        Mapping[str, date],
    ]

    def is_active(
        self,
        candidate: IndexCandidate,
        as_of_date: date,
    ) -> bool:
        if as_of_date < self.effective_start_date:
            return False
        symbol = candidate.symbol.removesuffix(".US")
        for membership in candidate.memberships:
            membership_intervals = self.intervals.get(
                membership,
                {},
            ).get(symbol, ())
            if any(
                interval.contains(as_of_date)
                for interval in membership_intervals
            ):
                return True
            override = self.snapshot_overrides.get(
                membership,
                {},
            ).get(symbol)
            if override is not None and as_of_date >= override:
                return True
        return False

    def coverage(
        self,
        candidates: Sequence[IndexCandidate],
    ) -> MembershipHistoryCoverage:
        authoritative = 0
        snapshot_only: list[str] = []
        missing: list[str] = []
        for candidate in candidates:
            symbol = candidate.symbol.removesuffix(".US")
            has_intervals = any(
                symbol in self.intervals.get(membership, {})
                for membership in candidate.memberships
            )
            has_override = any(
                symbol
                in self.snapshot_overrides.get(membership, {})
                for membership in candidate.memberships
            )
            if has_intervals:
                authoritative += 1
            elif has_override:
                snapshot_only.append(candidate.symbol)
            else:
                missing.append(candidate.symbol)
        return MembershipHistoryCoverage(
            catalog_size=len(candidates),
            authoritative_symbols=authoritative,
            snapshot_only_symbols=tuple(sorted(snapshot_only)),
            missing_symbols=tuple(sorted(missing)),
        )

    def metadata(
        self,
        candidates: Sequence[IndexCandidate],
    ) -> dict[str, object]:
        return {
            "source_version": self.source_version,
            "effective_start_date": (
                self.effective_start_date.isoformat()
            ),
            "catalog_snapshot_date": (
                self.catalog_snapshot_date.isoformat()
            ),
            "sources": [dict(source) for source in self.sources],
            **self.coverage(candidates).to_dict(),
        }


def _required_string(
    raw: Mapping[str, Any],
    key: str,
) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"membership history {key} is invalid")
    return value


def _parse_intervals(
    raw: Any,
) -> dict[str, dict[str, tuple[MembershipInterval, ...]]]:
    if not isinstance(raw, dict):
        raise ValueError("membership intervals are invalid")
    result: dict[
        str,
        dict[str, tuple[MembershipInterval, ...]],
    ] = {}
    for membership, raw_symbols in raw.items():
        if not isinstance(membership, str) or not isinstance(
            raw_symbols,
            dict,
        ):
            raise ValueError("membership interval index is invalid")
        symbols: dict[str, tuple[MembershipInterval, ...]] = {}
        for symbol, raw_rows in raw_symbols.items():
            if not isinstance(symbol, str) or not isinstance(
                raw_rows,
                list,
            ):
                raise ValueError(
                    "membership interval symbol is invalid"
                )
            rows: list[MembershipInterval] = []
            for raw_row in raw_rows:
                if (
                    not isinstance(raw_row, list)
                    or len(raw_row) != 2
                    or not all(
                        isinstance(value, str)
                        for value in raw_row
                    )
                ):
                    raise ValueError(
                        "membership interval row is invalid"
                    )
                start = date.fromisoformat(raw_row[0])
                end = (
                    date.fromisoformat(raw_row[1])
                    if raw_row[1]
                    else None
                )
                if end is not None and end <= start:
                    raise ValueError(
                        "membership interval bounds are invalid"
                    )
                rows.append(MembershipInterval(start, end))
            symbols[symbol] = tuple(rows)
        result[membership] = symbols
    return result


def _parse_overrides(
    raw: Any,
) -> dict[str, dict[str, date]]:
    if not isinstance(raw, dict):
        raise ValueError("membership snapshot overrides are invalid")
    result: dict[str, dict[str, date]] = {}
    for membership, raw_symbols in raw.items():
        if not isinstance(membership, str) or not isinstance(
            raw_symbols,
            dict,
        ):
            raise ValueError(
                "membership snapshot override index is invalid"
            )
        symbols: dict[str, date] = {}
        for symbol, raw_start in raw_symbols.items():
            if not isinstance(symbol, str) or not isinstance(
                raw_start,
                str,
            ):
                raise ValueError(
                    "membership snapshot override is invalid"
                )
            symbols[symbol] = date.fromisoformat(raw_start)
        result[membership] = symbols
    return result


@lru_cache(maxsize=1)
def load_index_membership_history() -> IndexMembershipHistory:
    raw = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("membership history root is invalid")
    raw_sources = raw.get("sources")
    if not isinstance(raw_sources, list):
        raise ValueError("membership history sources are invalid")
    sources: list[Mapping[str, str]] = []
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict) or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            for key, value in raw_source.items()
        ):
            raise ValueError(
                "membership history source is invalid"
            )
        sources.append(dict(raw_source))
    history = IndexMembershipHistory(
        source_version=_required_string(raw, "source_version"),
        effective_start_date=date.fromisoformat(
            _required_string(raw, "effective_start_date")
        ),
        catalog_snapshot_date=date.fromisoformat(
            _required_string(raw, "catalog_snapshot_date")
        ),
        sources=tuple(sources),
        intervals=_parse_intervals(raw.get("intervals")),
        snapshot_overrides=_parse_overrides(
            raw.get("snapshot_overrides")
        ),
    )
    return history


INDEX_MEMBERSHIP_HISTORY = load_index_membership_history()
