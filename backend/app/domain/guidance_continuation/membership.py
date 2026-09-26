"""Point-in-time membership resolution (PREREGISTRATION §10.2).

Pure computation: for each configured index, EXACTLY ONE applicable
baseline snapshot (the latest whose ``baseline_effective_date`` ≤ the
target day and which was obtained strictly before the target's 09:25 ET)
plus deterministically ordered change records determine whether a symbol
was in the base pool (NASDAQ_100 ∪ DJIA) on the target trading day.

Fail-closed tri-state semantics:
  * a baseline dated before the target proves membership ON ITS OWN DATE,
    not on the target day — resolving the target day (MEMBER or
    NOT_MEMBER alike) requires, for EACH index, a coverage proof that the
    index's change record is complete from ``baseline_effective_date``
    through the target day, obtained strictly before 09:25 ET.  An old
    snapshot is never valid forever;
  * a change that is effective ≤ target but was learned too late
    (``first_observed_at`` not before 09:25) makes that index UNKNOWN —
    the OLD status is never kept;
  * conflicting ADD/REMOVE records on the same effective date → UNKNOWN;
  * overall: MEMBER if ANY configured index resolves MEMBER (with valid
    coverage); NOT_MEMBER only if EVERY configured index resolves
    NOT_MEMBER (each with valid coverage); otherwise UNKNOWN;
  * missing sources never prove MEMBER or NOT_MEMBER.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Final
from zoneinfo import ZoneInfo

from app.domain.guidance_continuation.config import (
    DEFAULT_GUIDANCE_CONFIG,
    GuidanceContinuationConfig,
)

_ET = ZoneInfo("America/New_York")

MembershipStatus = str
MEMBER: Final[str] = "MEMBER"
NOT_MEMBER: Final[str] = "NOT_MEMBER"
UNKNOWN: Final[str] = "UNKNOWN"

CHANGE_ADD: Final[str] = "ADD"
CHANGE_REMOVE: Final[str] = "REMOVE"


@dataclass(frozen=True, slots=True)
class MembershipSnapshot:
    """A point-in-time index → symbols snapshot.

    ``obtained_at`` is when the system obtained (and retained) the
    snapshot; a snapshot supports a target day only if it was obtained
    strictly before that day's 09:25 ET freeze AND its
    ``baseline_effective_date`` is ≤ the target day.
    """

    index: str
    symbols: frozenset[str]
    baseline_effective_date: date
    obtained_at: datetime


@dataclass(frozen=True, slots=True)
class MembershipChange:
    """One index composition change (ADD or REMOVE)."""

    index: str
    symbol: str
    change_type: str  # CHANGE_ADD | CHANGE_REMOVE
    effective_date: date
    first_observed_at: datetime


@dataclass(frozen=True, slots=True)
class CoverageProof:
    """Per-index proof that the change record set is complete.

    ``index``: which index's change list this proof attests (each index's
    changes are sourced separately).  ``changes_complete_through``: the
    provider's change list is asserted complete UP TO and INCLUDING this
    date; ``obtained_at`` is when the system obtained that proof.  A
    proof supports a target day only if it attests the index's changes
    through the target day AND was obtained strictly before the target's
    09:25 ET freeze — this is required for BOTH MEMBER and NOT_MEMBER
    resolutions (§10.2 L439-446).
    """

    index: str
    changes_complete_through: date
    obtained_at: datetime


@dataclass(frozen=True, slots=True)
class MembershipVerdict:
    """Tri-state membership result plus the reason for non-membership."""

    status: MembershipStatus
    reason: str


def registration_deadline(target_day: date) -> datetime:
    """09:25 ET on the target trading day (§10.2/§10.3 freeze boundary)."""
    return datetime.combine(target_day, time(9, 25), tzinfo=_ET)


def _is_visible(instant: datetime, target_day: date) -> bool:
    """Whether ``instant`` was available before the day's 09:25 ET freeze."""
    return instant < registration_deadline(target_day)


def _applicable_baseline(
    index: str,
    baselines: tuple[MembershipSnapshot, ...],
    target_day: date,
) -> MembershipSnapshot | None:
    """The ONE latest baseline dated ≤ target and obtained before 09:25.

    Older or later-obtained snapshots for the same index are ignored
    entirely — a stale older snapshot can never resurrect a status a newer
    snapshot dropped, and a snapshot obtained after the freeze supports
    nothing.  If two or more timely snapshots share the LATEST effective
    date but disagree on content, that index is UNKNOWABLE (None): exact
    duplicates deduplicate, any difference is a conflict, and selection
    never depends on input order (R2a).
    """
    candidates = [
        b
        for b in baselines
        if b.index == index
        and b.baseline_effective_date <= target_day
        and _is_visible(b.obtained_at, target_day)
    ]
    if not candidates:
        return None
    latest_date = max(b.baseline_effective_date for b in candidates)
    latest = [b for b in candidates if b.baseline_effective_date == latest_date]
    distinct = {b.symbols for b in latest}
    if len(distinct) > 1:
        return None  # conflicting same-date snapshots → UNKNOWN
    return latest[0]


def _valid_proof(
    proof: CoverageProof | None,
    target_day: date,
) -> bool:
    """Whether a single proof covers the index for ``target_day``."""
    if proof is None:
        return False
    return (
        proof.changes_complete_through >= target_day
        and _is_visible(proof.obtained_at, target_day)
    )


def _index_covered(
    proofs_for_index: list[CoverageProof],
    target_day: date,
) -> bool:
    """Coverage rule for one index, order-independent (R2b).

    The index is covered if ANY proof for it was obtained strictly before
    the target's 09:25 ET freeze AND attests changes complete through the
    target day.  Late or short proofs are IGNORED — they never override a
    valid one, whichever order the proofs arrive in.  (A late proof
    cannot retroactively un-know what a timely proof already covered.)
    """
    return any(_valid_proof(p, target_day) for p in proofs_for_index)


def _strongest_proof(
    proofs_for_index: list[CoverageProof],
    target_day: date,
) -> CoverageProof | None:
    """A deterministic valid proof for the index (used for wiring only).

    With the ANY-rule applied by :func:`_index_covered`, the choice among
    valid proofs cannot change the verdict; pick deterministically (the
    one attesting the furthest completion, then the earliest obtained) so
    behaviour never depends on input order.
    """
    valid = [p for p in proofs_for_index if _valid_proof(p, target_day)]
    if not valid:
        return None
    return sorted(
        valid,
        key=lambda p: (-p.changes_complete_through.toordinal(), p.obtained_at),
    )[0]


def _resolve_index(
    *,
    symbol: str,
    index: str,
    baseline: MembershipSnapshot,
    changes: tuple[MembershipChange, ...],
    target_day: date,
    coverage: CoverageProof | None,
) -> bool | None:
    """Resolve one index's membership at ``target_day``; None = unknowable.

    Coverage is required SYMMETRICALLY: a baseline dated before the
    target proves membership only on its own date, so both MEMBER and
    NOT_MEMBER need a per-index proof that changes are complete from the
    baseline date through the target day, obtained before 09:25 ET.
    """
    # Coverage gate first: without a valid proof for this index, the
    # baseline cannot speak about the target day at all.
    if coverage is None:
        return None
    if coverage.changes_complete_through < target_day:
        return None
    if not _is_visible(coverage.obtained_at, target_day):
        return None

    applicable = [
        c
        for c in changes
        if c.index == index
        and baseline.baseline_effective_date < c.effective_date <= target_day
    ]

    # A change that is effective but learned too late: we cannot know the
    # true composition → UNKNOWN (fail closed), never keep the old status.
    for change in applicable:
        if not _is_visible(change.first_observed_at, target_day):
            return None

    # Deterministic order regardless of record order.
    ordered = sorted(
        applicable, key=lambda c: (c.effective_date, c.first_observed_at)
    )

    # Conflicting ADD/REMOVE for the same (symbol, effective_date).
    seen: dict[tuple[str, date], str] = {}
    for change in ordered:
        key = (change.symbol, change.effective_date)
        if key in seen and seen[key] != change.change_type:
            return None
        seen[key] = change.change_type

    member = set(baseline.symbols)
    for change in ordered:
        if change.change_type == CHANGE_ADD:
            member.add(change.symbol)
        elif change.change_type == CHANGE_REMOVE:
            member.discard(change.symbol)

    return symbol in member


def evaluate_membership(
    *,
    symbol: str,
    target_day: date,
    baselines: tuple[MembershipSnapshot, ...],
    changes: tuple[MembershipChange, ...] = (),
    coverage: tuple[CoverageProof, ...] = (),
    config: GuidanceContinuationConfig = DEFAULT_GUIDANCE_CONFIG,
) -> MembershipVerdict:
    """Resolve (symbol, target_day) membership in the §10.2 base pool.

    Per index (order-independent at every step):
      1. Baseline: exactly one applicable snapshot — the latest
         ``baseline_effective_date`` ≤ target among snapshots obtained
         before 09:25 ET.  Conflicting same-date snapshots → UNKNOWN;
         exact duplicates deduplicate.
      2. Coverage: the index is covered if ANY per-index proof attests
         changes complete through the target day AND was obtained
         before 09:25 ET.  Late or short proofs are ignored, never
         overriding a valid one.  Coverage is required for BOTH MEMBER
         and NOT_MEMBER resolutions.
      3. Changes: applied after the baseline date through the target,
         ordered by (effective_date, first_observed_at); late-learned or
         same-date conflicting changes → UNKNOWN.

    Overall: MEMBER if ANY configured index resolves MEMBER with valid
    coverage; NOT_MEMBER only if EVERY configured index resolves
    NOT_MEMBER with valid coverage; otherwise UNKNOWN — missing sources
    never prove MEMBER or NOT_MEMBER (§10.2 L439-446).
    """
    _ = config  # thresholds live in the config; none are index-specific
    proofs_by_index: dict[str, list[CoverageProof]] = {}
    for p in coverage:
        proofs_by_index.setdefault(p.index, []).append(p)
    statuses: list[bool | None] = []
    for index in config.universe_indices:
        baseline = _applicable_baseline(index, baselines, target_day)
        if baseline is None:
            statuses.append(None)
            continue
        if not _index_covered(proofs_by_index.get(index, []), target_day):
            statuses.append(None)
            continue
        statuses.append(
            _resolve_index(
                symbol=symbol,
                index=index,
                baseline=baseline,
                changes=changes,
                target_day=target_day,
                coverage=_strongest_proof(
                    proofs_by_index[index], target_day
                ),
            )
        )

    if any(status is True for status in statuses):
        return MembershipVerdict(status=MEMBER, reason="")
    if any(status is None for status in statuses):
        missing: list[str] = []
        for index in config.universe_indices:
            baseline = _applicable_baseline(index, baselines, target_day)
            if baseline is None:
                missing.append(f"{index}:no-applicable-baseline")
                continue
            proofs = proofs_by_index.get(index, [])
            if not _index_covered(proofs, target_day):
                missing.append(f"{index}:no-valid-coverage")
                continue
            resolved = _resolve_index(
                symbol=symbol,
                index=index,
                baseline=baseline,
                changes=changes,
                target_day=target_day,
                coverage=_strongest_proof(proofs, target_day),
            )
            if resolved is None:
                missing.append(f"{index}:late-or-conflicting-change")
        return MembershipVerdict(
            status=UNKNOWN,
            reason="membership cannot be proven from data available before "
            "the 09:25 ET freeze: " + ", ".join(missing),
        )
    return MembershipVerdict(
        status=NOT_MEMBER,
        reason="symbol absent from all configured universe indices at "
        "target_day",
    )
