"""Point-in-time membership resolution (PREREGISTRATION §10.2)."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app.domain.guidance_continuation.membership import (
    CHANGE_ADD,
    CHANGE_REMOVE,
    MEMBER,
    NOT_MEMBER,
    UNKNOWN,
    CoverageProof,
    MembershipChange,
    MembershipSnapshot,
    evaluate_membership,
    registration_deadline,
)

_ET = ZoneInfo("America/New_York")

TARGET_DAY = date(2026, 9, 22)

BASE_NDX = MembershipSnapshot(
    index="NASDAQ_100",
    symbols=frozenset({"AAPL", "MSFT", "NVDA"}),
    baseline_effective_date=date(2026, 1, 1),
    obtained_at=datetime(2026, 1, 5, 12, 0, tzinfo=_ET),
)
BASE_DJIA = MembershipSnapshot(
    index="DJIA",
    symbols=frozenset({"AAPL", "IBM"}),
    baseline_effective_date=date(2026, 1, 1),
    obtained_at=datetime(2026, 1, 5, 12, 0, tzinfo=_ET),
)
BASELINES = (BASE_NDX, BASE_DJIA)

# Per-index coverage proofs used by legacy cases: each index's change
# list is complete through the target day, obtained the prior evening —
# required for BOTH MEMBER and NOT_MEMBER resolutions (B3 residual).
_COVERED = (
    CoverageProof(
        index="NASDAQ_100",
        changes_complete_through=TARGET_DAY,
        obtained_at=datetime(2026, 9, 21, 18, 0, tzinfo=_ET),
    ),
    CoverageProof(
        index="DJIA",
        changes_complete_through=TARGET_DAY,
        obtained_at=datetime(2026, 9, 21, 18, 0, tzinfo=_ET),
    ),
)


class TestB3Residual:
    """Gate-1 B3 residual: coverage is required for MEMBER too.

    A baseline dated 2026-07-24 proves membership on 07-24, not on the
    target day: nothing proves the symbol was not removed between the
    baseline date and the target.  Per §10.2 L439-446, membership on the
    target day needs BOTH the baseline AND proof that the change record
    is complete from baseline_effective_date through target_day, obtained
    before 09:25 ET.  An old snapshot is never valid forever.
    """

    def test_b3_residual_no_coverage_member_is_unknown(self) -> None:
        # The reviewer's repro: NDX baseline dated 2026-07-24 containing
        # NVDA.US, DJIA baseline without it, coverage=None, target
        # 2026-11-03.  Old code returned MEMBER; must be UNKNOWN.
        ndx = MembershipSnapshot(
            index="NASDAQ_100",
            symbols=frozenset({"NVDA.US"}),
            baseline_effective_date=date(2026, 7, 24),
            obtained_at=datetime(2026, 7, 24, 12, 0, tzinfo=_ET),
        )
        djia = MembershipSnapshot(
            index="DJIA",
            symbols=frozenset(),
            baseline_effective_date=date(2026, 7, 24),
            obtained_at=datetime(2026, 7, 24, 12, 0, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="NVDA.US",
            target_day=date(2026, 11, 3),
            baselines=(ndx, djia),
        )
        assert verdict.status == UNKNOWN

    def test_b3_residual_member_with_coverage(self) -> None:
        # Same data plus per-index coverage reaching the target day and
        # obtained before 09:25 → MEMBER is provable again.
        ndx = MembershipSnapshot(
            index="NASDAQ_100",
            symbols=frozenset({"NVDA.US"}),
            baseline_effective_date=date(2026, 7, 24),
            obtained_at=datetime(2026, 7, 24, 12, 0, tzinfo=_ET),
        )
        djia = MembershipSnapshot(
            index="DJIA",
            symbols=frozenset(),
            baseline_effective_date=date(2026, 7, 24),
            obtained_at=datetime(2026, 7, 24, 12, 0, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="NVDA.US",
            target_day=date(2026, 11, 3),
            baselines=(ndx, djia),
            coverage=(
                CoverageProof(
                    index="NASDAQ_100",
                    changes_complete_through=date(2026, 11, 3),
                    obtained_at=datetime(2026, 11, 2, 18, 0, tzinfo=_ET),
                ),
                CoverageProof(
                    index="DJIA",
                    changes_complete_through=date(2026, 11, 3),
                    obtained_at=datetime(2026, 11, 2, 18, 0, tzinfo=_ET),
                ),
            ),
        )
        assert verdict.status == MEMBER

    def test_b3_residual_member_needs_coverage_from_baseline_date(
        self,
    ) -> None:
        # Coverage reaching only 2026-09-01 (< baseline 07-24? No — must
        # cover the (baseline, target] span): completeness through a date
        # BEFORE the baseline date is insufficient; through a date short
        # of the target is insufficient.  Both → UNKNOWN.
        ndx = MembershipSnapshot(
            index="NASDAQ_100",
            symbols=frozenset({"NVDA.US"}),
            baseline_effective_date=date(2026, 7, 24),
            obtained_at=datetime(2026, 7, 24, 12, 0, tzinfo=_ET),
        )
        djia = MembershipSnapshot(
            index="DJIA",
            symbols=frozenset(),
            baseline_effective_date=date(2026, 7, 24),
            obtained_at=datetime(2026, 7, 24, 12, 0, tzinfo=_ET),
        )
        short = evaluate_membership(
            symbol="NVDA.US",
            target_day=date(2026, 11, 3),
            baselines=(ndx, djia),
            coverage=(
                CoverageProof(
                    index="NASDAQ_100",
                    changes_complete_through=date(2026, 9, 1),
                    obtained_at=datetime(2026, 9, 1, 18, 0, tzinfo=_ET),
                ),
                CoverageProof(
                    index="DJIA",
                    changes_complete_through=date(2026, 11, 3),
                    obtained_at=datetime(2026, 11, 2, 18, 0, tzinfo=_ET),
                ),
            ),
        )
        assert short.status == UNKNOWN

    def test_b3_residual_per_index_member_one_index_covered(self) -> None:
        # NASDAQ_100 resolves MEMBER with valid coverage; DJIA has NO
        # coverage (its status is UNKNOWN).  Any covered MEMBER wins →
        # overall MEMBER.
        ndx = MembershipSnapshot(
            index="NASDAQ_100",
            symbols=frozenset({"NVDA.US"}),
            baseline_effective_date=date(2026, 7, 24),
            obtained_at=datetime(2026, 7, 24, 12, 0, tzinfo=_ET),
        )
        djia = MembershipSnapshot(
            index="DJIA",
            symbols=frozenset(),
            baseline_effective_date=date(2026, 7, 24),
            obtained_at=datetime(2026, 7, 24, 12, 0, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="NVDA.US",
            target_day=date(2026, 11, 3),
            baselines=(ndx, djia),
            coverage=(
                CoverageProof(
                    index="NASDAQ_100",
                    changes_complete_through=date(2026, 11, 3),
                    obtained_at=datetime(2026, 11, 2, 18, 0, tzinfo=_ET),
                ),
                # DJIA: no proof at all.
            ),
        )
        assert verdict.status == MEMBER

    def test_b3_residual_not_member_one_index_uncovered(self) -> None:
        # Both indices resolve NOT_MEMBER over their baselines, but DJIA
        # has no coverage — NOT_MEMBER requires EVERY index covered →
        # overall UNKNOWN.
        ndx = MembershipSnapshot(
            index="NASDAQ_100",
            symbols=frozenset({"AAPL"}),
            baseline_effective_date=date(2026, 7, 24),
            obtained_at=datetime(2026, 7, 24, 12, 0, tzinfo=_ET),
        )
        djia = MembershipSnapshot(
            index="DJIA",
            symbols=frozenset(),
            baseline_effective_date=date(2026, 7, 24),
            obtained_at=datetime(2026, 7, 24, 12, 0, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="TSLA.US",
            target_day=date(2026, 11, 3),
            baselines=(ndx, djia),
            coverage=(
                CoverageProof(
                    index="NASDAQ_100",
                    changes_complete_through=date(2026, 11, 3),
                    obtained_at=datetime(2026, 11, 2, 18, 0, tzinfo=_ET),
                ),
                # DJIA: no proof.
            ),
        )
        assert verdict.status == UNKNOWN


class TestR2OrderIndependence:
    """R2: neither baseline selection nor coverage selection may depend on
    input order.  Conflicts → UNKNOWN; exact duplicates dedupe; a late or
    short coverage proof is IGNORED, never overriding a valid one."""

    TARGET = date(2026, 11, 3)

    def _base(self, index: str, symbols: frozenset[str]) -> MembershipSnapshot:
        return MembershipSnapshot(
            index=index,
            symbols=symbols,
            baseline_effective_date=date(2026, 7, 24),
            obtained_at=datetime(2026, 7, 24, 12, 0, tzinfo=_ET),
        )

    def _cov(
        self, index: str, obtained: datetime, through: date | None = None
    ) -> CoverageProof:
        return CoverageProof(
            index=index,
            changes_complete_through=through or self.TARGET,
            obtained_at=obtained,
        )

    def test_r2a_conflicting_same_date_baselines_all_permutations(self) -> None:
        # Two timely NASDAQ_100 baselines with the SAME effective date,
        # one containing NVDA.US and one not → index UNKNOWN regardless of
        # input order (previously max() took the first on a tie).
        with_nvda = self._base("NASDAQ_100", frozenset({"NVDA.US"}))
        without = self._base("NASDAQ_100", frozenset())
        djia = self._base("DJIA", frozenset())
        cov = (
            self._cov("NASDAQ_100", datetime(2026, 11, 2, 18, 0, tzinfo=_ET)),
            self._cov("DJIA", datetime(2026, 11, 2, 18, 0, tzinfo=_ET)),
        )
        import itertools

        for perm in itertools.permutations((with_nvda, without, djia)):
            verdict = evaluate_membership(
                symbol="NVDA.US",
                target_day=self.TARGET,
                baselines=perm,
                coverage=cov,
            )
            assert verdict.status == UNKNOWN, (
                f"order {[b.index for b in perm]} gave {verdict.status}"
            )

    def test_r2a_exact_duplicate_baselines_dedupe(self) -> None:
        # Two EXACT copies of the same baseline are one snapshot → MEMBER.
        a = self._base("NASDAQ_100", frozenset({"NVDA.US"}))
        dup = self._base("NASDAQ_100", frozenset({"NVDA.US"}))
        djia = self._base("DJIA", frozenset())
        cov = (
            self._cov("NASDAQ_100", datetime(2026, 11, 2, 18, 0, tzinfo=_ET)),
            self._cov("DJIA", datetime(2026, 11, 2, 18, 0, tzinfo=_ET)),
        )
        for perm in ((a, dup, djia), (dup, a, djia)):
            verdict = evaluate_membership(
                symbol="NVDA.US",
                target_day=self.TARGET,
                baselines=perm,
                coverage=cov,
            )
            assert verdict.status == MEMBER

    def test_r2b_late_proof_ignored_valid_one_wins_all_permutations(self) -> None:
        # Two proofs for NASDAQ_100: one timely (09:00) and one late
        # (09:30).  The index is covered if ANY proof is timely AND
        # reaches the target — the late one is ignored, so BOTH orders
        # resolve MEMBER (previously the dict kept the last entry).
        timely = self._cov(
            "NASDAQ_100", datetime(2026, 11, 3, 9, 0, tzinfo=_ET)
        )
        late = self._cov(
            "NASDAQ_100", datetime(2026, 11, 3, 9, 30, tzinfo=_ET)
        )
        djia_cov = self._cov("DJIA", datetime(2026, 11, 2, 18, 0, tzinfo=_ET))
        bases = (
            self._base("NASDAQ_100", frozenset({"NVDA.US"})),
            self._base("DJIA", frozenset()),
        )
        for cov in ((timely, late, djia_cov), (late, timely, djia_cov)):
            verdict = evaluate_membership(
                symbol="NVDA.US",
                target_day=self.TARGET,
                baselines=bases,
                coverage=cov,
            )
            assert verdict.status == MEMBER, f"cov order gave {verdict.status}"

    def test_r2b_short_proof_ignored_valid_one_wins(self) -> None:
        # A proof reaching only 2026-09-01 is ignored; one reaching the
        # target still covers the index — order-independent MEMBER.
        short = self._cov(
            "NASDAQ_100",
            datetime(2026, 11, 2, 18, 0, tzinfo=_ET),
            through=date(2026, 9, 1),
        )
        full = self._cov(
            "NASDAQ_100", datetime(2026, 11, 2, 18,  0, tzinfo=_ET)
        )
        djia_cov = self._cov("DJIA", datetime(2026, 11, 2, 18, 0, tzinfo=_ET))
        bases = (
            self._base("NASDAQ_100", frozenset({"NVDA.US"})),
            self._base("DJIA", frozenset()),
        )
        for cov in ((short, full, djia_cov), (full, short, djia_cov)):
            verdict = evaluate_membership(
                symbol="NVDA.US",
                target_day=self.TARGET,
                baselines=bases,
                coverage=cov,
            )
            assert verdict.status == MEMBER

    def test_r2b_only_late_proofs_unknown(self) -> None:
        # No timely proof at all → the index stays UNKNOWN.
        late1 = self._cov(
            "NASDAQ_100", datetime(2026, 11, 3, 9, 25, tzinfo=_ET)
        )
        late2 = self._cov(
            "NASDAQ_100", datetime(2026, 11, 3, 9, 30, tzinfo=_ET)
        )
        djia_cov = self._cov("DJIA", datetime(2026, 11, 2, 18, 0, tzinfo=_ET))
        bases = (
            self._base("NASDAQ_100", frozenset({"NVDA.US"})),
            self._base("DJIA", frozenset()),
        )
        for cov in ((late1, djia_cov), (late2, late1, djia_cov)):
            verdict = evaluate_membership(
                symbol="NVDA.US",
                target_day=self.TARGET,
                baselines=bases,
                coverage=cov,
            )
            assert verdict.status == UNKNOWN


def _change(
    index: str,
    symbol: str,
    change_type: str,
    effective: date,
    observed: datetime,
) -> MembershipChange:
    return MembershipChange(
        index=index,
        symbol=symbol,
        change_type=change_type,
        effective_date=effective,
        first_observed_at=observed,
    )


class TestMembershipB3:
    """B3: exactly one applicable baseline, coverage proof, fail-closed."""

    def test_b3_no_baseline_is_unknown_not_not_member(self) -> None:
        verdict = evaluate_membership(
            symbol="AAPL", target_day=TARGET_DAY, baselines=()
        )
        assert verdict.status == UNKNOWN

    def test_b3_order_independent_remove_then_add_records(self) -> None:
        # Passing a newer REMOVE and then an OLDER ADD (record order) must
        # not flip the outcome vs the sorted order: deterministic
        # (effective_date, first_observed_at) ordering.
        add = _change(
            "NASDAQ_100", "NVDA", CHANGE_ADD, date(2026, 9, 1),
            datetime(2026, 9, 1, 18, 0, tzinfo=_ET),
        )
        remove = _change(
            "NASDAQ_100", "NVDA", CHANGE_REMOVE, date(2026, 9, 15),
            datetime(2026, 9, 15, 18, 0, tzinfo=_ET),
        )
        forward = evaluate_membership(
            symbol="NVDA", target_day=TARGET_DAY, baselines=BASELINES,
            changes=(add, remove), coverage=_COVERED,
        )
        backward = evaluate_membership(
            symbol="NVDA", target_day=TARGET_DAY, baselines=BASELINES,
            changes=(remove, add), coverage=_COVERED,
        )
        assert forward.status == backward.status == NOT_MEMBER

    def test_b3_older_baseline_dropped_after_newer_snapshot(self) -> None:
        # A newer baseline snapshot (later effective date) that DROPS the
        # symbol supersedes an older one that had it: still MEMBER via the
        # older?  No — exactly ONE baseline applies: the latest whose
        # baseline date ≤ target and obtained before 09:25.
        old_base = MembershipSnapshot(
            index="NASDAQ_100",
            symbols=frozenset({"AAPL", "NVDA"}),
            baseline_effective_date=date(2026, 1, 1),
            obtained_at=datetime(2026, 1, 5, 12, 0, tzinfo=_ET),
        )
        new_base = MembershipSnapshot(
            index="NASDAQ_100",
            symbols=frozenset(),  # NVDA dropped
            baseline_effective_date=date(2026, 9, 1),
            obtained_at=datetime(2026, 9, 2, 12, 0, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="NVDA",
            target_day=TARGET_DAY,
            baselines=(old_base, new_base, BASE_DJIA),
            changes=(),
            coverage=_COVERED,
        )
        assert verdict.status == NOT_MEMBER

    def test_b3_stale_older_baseline_cannot_override_newer(self) -> None:
        # Older snapshot still MEMBER after a newer snapshot dropped the
        # symbol: the newer baseline wins; the older must not resurrect it.
        new_base = MembershipSnapshot(
            index="NASDAQ_100",
            symbols=frozenset(),
            baseline_effective_date=date(2026, 9, 1),
            obtained_at=datetime(2026, 9, 2, 12, 0, tzinfo=_ET),
        )
        old_base = MembershipSnapshot(
            index="NASDAQ_100",
            symbols=frozenset({"NVDA"}),
            baseline_effective_date=date(2026, 1, 1),
            obtained_at=datetime(2026, 1, 5, 12, 0, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="NVDA",
            target_day=TARGET_DAY,
            baselines=(new_base, old_base, BASE_DJIA),
            changes=(),
            coverage=_COVERED,
        )
        assert verdict.status == NOT_MEMBER

    def test_b3_effective_remove_learned_late_is_unknown(self) -> None:
        # REMOVE effective 2026-09-10 (before target) but first observed
        # 2026-09-22 09:30 ET — after the freeze.  We cannot know whether
        # NVDA was still a member: UNKNOWN (fail closed), never keep the
        # old MEMBER status.  Valid per-index coverage is supplied so the
        # LATE-CHANGE branch — not the missing-coverage branch — is what
        # produces the verdict.
        late_remove = _change(
            "NASDAQ_100", "NVDA", CHANGE_REMOVE, date(2026, 9, 10),
            datetime(2026, 9, 22, 9, 30, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="NVDA", target_day=TARGET_DAY, baselines=BASELINES,
            changes=(late_remove,), coverage=_COVERED,
        )
        assert verdict.status == UNKNOWN
        assert "NASDAQ_100:late-or-conflicting-change" in verdict.reason

    def test_b3_coverage_proof_incomplete_is_unknown(self) -> None:
        # changes_complete_through 2026-09-01 < target 2026-09-22: symbol
        # absent from the baselines, and the incomplete coverage cannot
        # prove no ADD happened after 09-01 → UNKNOWN.
        ndx = MembershipSnapshot(
            index="NASDAQ_100",
            symbols=frozenset({"AAPL"}),
            baseline_effective_date=date(2026, 1, 1),
            obtained_at=datetime(2026, 1, 5, 12, 0, tzinfo=_ET),
        )
        djia = MembershipSnapshot(
            index="DJIA",
            symbols=frozenset(),
            baseline_effective_date=date(2026, 1, 1),
            obtained_at=datetime(2026, 1, 5, 12, 0, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="TSLA",
            target_day=TARGET_DAY,
            baselines=(ndx, djia),
            changes=(),
            coverage=(
                CoverageProof(
                    index="NASDAQ_100",
                    changes_complete_through=date(2026, 9, 1),
                    obtained_at=datetime(2026, 9, 21, 12, 0, tzinfo=_ET),
                ),
                CoverageProof(
                    index="DJIA",
                    changes_complete_through=date(2026, 9, 1),
                    obtained_at=datetime(2026, 9, 21, 12, 0, tzinfo=_ET),
                ),
            ),
        )
        assert verdict.status == UNKNOWN

    def test_b3_coverage_proof_complete_allows_not_member(self) -> None:
        # Coverage through the target day, obtained before 09:25, symbol
        # absent from every configured index's applicable baseline →
        # NOT_MEMBER is provable.
        ndx = MembershipSnapshot(
            index="NASDAQ_100",
            symbols=frozenset({"AAPL"}),
            baseline_effective_date=date(2026, 1, 1),
            obtained_at=datetime(2026, 1, 5, 12, 0, tzinfo=_ET),
        )
        djia = MembershipSnapshot(
            index="DJIA",
            symbols=frozenset(),
            baseline_effective_date=date(2026, 1, 1),
            obtained_at=datetime(2026, 1, 5, 12, 0, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="TSLA",
            target_day=TARGET_DAY,
            baselines=(ndx, djia),
            changes=(),
            coverage=(
                CoverageProof(
                    index="NASDAQ_100",
                    changes_complete_through=TARGET_DAY,
                    obtained_at=datetime(2026, 9, 21, 12, 0, tzinfo=_ET),
                ),
                CoverageProof(
                    index="DJIA",
                    changes_complete_through=TARGET_DAY,
                    obtained_at=datetime(2026, 9, 21, 12, 0, tzinfo=_ET),
                ),
            ),
        )
        assert verdict.status == NOT_MEMBER

    def test_b3_coverage_proof_obtained_late_is_unknown(self) -> None:
        # Coverage claims completeness but was only obtained AFTER the
        # 09:25 freeze → cannot prove anything → UNKNOWN.
        ndx = MembershipSnapshot(
            index="NASDAQ_100",
            symbols=frozenset({"AAPL"}),
            baseline_effective_date=date(2026, 1, 1),
            obtained_at=datetime(2026, 1, 5, 12, 0, tzinfo=_ET),
        )
        djia = MembershipSnapshot(
            index="DJIA",
            symbols=frozenset(),
            baseline_effective_date=date(2026, 1, 1),
            obtained_at=datetime(2026, 1, 5, 12, 0, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="TSLA",
            target_day=TARGET_DAY,
            baselines=(ndx, djia),
            changes=(),
            coverage=(
                CoverageProof(
                    index="NASDAQ_100",
                    changes_complete_through=TARGET_DAY,
                    obtained_at=datetime(2026, 9, 22, 9, 30, tzinfo=_ET),
                ),
                CoverageProof(
                    index="DJIA",
                    changes_complete_through=TARGET_DAY,
                    obtained_at=datetime(2026, 9, 22, 9, 30, tzinfo=_ET),
                ),
            ),
        )
        assert verdict.status == UNKNOWN

    def test_b3_conflicting_changes_same_effective_date_unknown(self) -> None:
        # ADD and REMOVE both effective 2026-09-10 for the same symbol:
        # contradictory records → UNKNOWN.  Valid per-index coverage is
        # supplied so the CONFLICT branch — not the missing-coverage
        # branch — is what produces the verdict.
        add = _change(
            "NASDAQ_100", "TSLA", CHANGE_ADD, date(2026, 9, 10),
            datetime(2026, 9, 10, 18, 0, tzinfo=_ET),
        )
        remove = _change(
            "NASDAQ_100", "TSLA", CHANGE_REMOVE, date(2026, 9, 10),
            datetime(2026, 9, 10, 18, 0, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="TSLA", target_day=TARGET_DAY, baselines=BASELINES,
            changes=(add, remove), coverage=_COVERED,
        )
        assert verdict.status == UNKNOWN
        assert "NASDAQ_100:late-or-conflicting-change" in verdict.reason


class TestDeadline:
    def test_deadline_is_0925_et(self) -> None:
        assert registration_deadline(TARGET_DAY) == datetime(
            2026, 9, 22, 9, 25, tzinfo=_ET
        )


class TestMembership:
    def test_symbol_in_one_index_is_member(self) -> None:
        verdict = evaluate_membership(
            symbol="NVDA",
            target_day=TARGET_DAY,
            baselines=BASELINES,
            coverage=_COVERED,
        )
        assert verdict.status == MEMBER

    def test_symbol_in_both_indices_is_member(self) -> None:
        verdict = evaluate_membership(
            symbol="AAPL",
            target_day=TARGET_DAY,
            baselines=BASELINES,
            coverage=_COVERED,
        )
        assert verdict.status == MEMBER

    def test_symbol_absent_everywhere_is_not_member(self) -> None:
        # NOT_MEMBER needs a coverage proof reaching the target day.
        verdict = evaluate_membership(
            symbol="TSLA",
            target_day=TARGET_DAY,
            baselines=BASELINES,
            coverage=_COVERED,
        )
        assert verdict.status == NOT_MEMBER
        assert verdict.reason

    def test_symbol_absent_without_coverage_is_unknown(self) -> None:
        # Same data, no coverage proof: UNKNOWN, never NOT_MEMBER.
        verdict = evaluate_membership(
            symbol="TSLA", target_day=TARGET_DAY, baselines=BASELINES
        )
        assert verdict.status == UNKNOWN

    def test_target_before_baseline_is_unknown(self) -> None:
        verdict = evaluate_membership(
            symbol="AAPL",
            target_day=date(2025, 12, 31),
            baselines=BASELINES,
            coverage=(
                CoverageProof(
                    index="NASDAQ_100",
                    changes_complete_through=date(2025, 12, 31),
                    obtained_at=datetime(2025, 12, 30, 18, 0, tzinfo=_ET),
                ),
                CoverageProof(
                    index="DJIA",
                    changes_complete_through=date(2025, 12, 31),
                    obtained_at=datetime(2025, 12, 30, 18, 0, tzinfo=_ET),
                ),
            ),
        )
        assert verdict.status == UNKNOWN
        assert verdict.reason

    def test_baseline_obtained_after_deadline_is_unknown(self) -> None:
        stale = MembershipSnapshot(
            index="NASDAQ_100",
            symbols=frozenset({"TSLA"}),
            baseline_effective_date=date(2026, 1, 1),
            obtained_at=datetime(2026, 9, 22, 9, 25, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="TSLA",
            target_day=TARGET_DAY,
            baselines=(stale,),
            coverage=_COVERED,
        )
        assert verdict.status == UNKNOWN

    def test_baseline_obtained_just_before_deadline_is_usable(self) -> None:
        fresh = MembershipSnapshot(
            index="NASDAQ_100",
            symbols=frozenset({"TSLA"}),
            baseline_effective_date=date(2026, 1, 1),
            obtained_at=datetime(2026, 9, 22, 9, 24, 59, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="TSLA",
            target_day=TARGET_DAY,
            baselines=(fresh,),
            coverage=_COVERED,
        )
        assert verdict.status == MEMBER


class TestChanges:
    def test_add_effective_today_counts(self) -> None:
        change = _change(
            "NASDAQ_100",
            "TSLA",
            CHANGE_ADD,
            date(2026, 9, 22),
            datetime(2026, 9, 20, 12, 0, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="TSLA",
            target_day=TARGET_DAY,
            baselines=BASELINES,
            changes=(change,),
            coverage=_COVERED,
        )
        assert verdict.status == MEMBER

    def test_add_effective_tomorrow_does_not_count(self) -> None:
        change = _change(
            "NASDAQ_100",
            "TSLA",
            CHANGE_ADD,
            date(2026, 9, 23),
            datetime(2026, 9, 20, 12, 0, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="TSLA",
            target_day=TARGET_DAY,
            baselines=BASELINES,
            changes=(change,),
            coverage=_COVERED,
        )
        assert verdict.status == NOT_MEMBER

    def test_add_observed_after_deadline_is_unknown_fail_closed(self) -> None:
        # B3: an ADD effective before the target but learned at/after the
        # freeze makes the composition unknowable → UNKNOWN, never the
        # pre-change status (fail closed).
        change = _change(
            "NASDAQ_100",
            "TSLA",
            CHANGE_ADD,
            date(2026, 9, 20),
            datetime(2026, 9, 22, 9, 25, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="TSLA",
            target_day=TARGET_DAY,
            baselines=BASELINES,
            changes=(change,),
            coverage=_COVERED,
        )
        assert verdict.status == UNKNOWN

    def test_add_observed_just_before_deadline_counts(self) -> None:
        change = _change(
            "NASDAQ_100",
            "TSLA",
            CHANGE_ADD,
            date(2026, 9, 20),
            datetime(2026, 9, 22, 9, 24, 59, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="TSLA",
            target_day=TARGET_DAY,
            baselines=BASELINES,
            changes=(change,),
            coverage=_COVERED,
        )
        assert verdict.status == MEMBER

    def test_remove_leaves_not_member(self) -> None:
        change = _change(
            "NASDAQ_100",
            "NVDA",
            CHANGE_REMOVE,
            date(2026, 9, 1),
            datetime(2026, 9, 1, 18, 0, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="NVDA",
            target_day=TARGET_DAY,
            baselines=BASELINES,
            changes=(change,),
            coverage=_COVERED,
        )
        assert verdict.status == NOT_MEMBER

    def test_remove_then_add_on_later_day(self) -> None:
        remove = _change(
            "NASDAQ_100",
            "NVDA",
            CHANGE_REMOVE,
            date(2026, 9, 1),
            datetime(2026, 9, 1, 18, 0, tzinfo=_ET),
        )
        readd = _change(
            "NASDAQ_100",
            "NVDA",
            CHANGE_ADD,
            date(2026, 9, 15),
            datetime(2026, 9, 15, 18, 0, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="NVDA",
            target_day=TARGET_DAY,
            baselines=BASELINES,
            changes=(remove, readd),
            coverage=_COVERED,
        )
        assert verdict.status == MEMBER

    def test_membership_in_either_index_suffices(self) -> None:
        # Removed from NDX but present in DJIA.
        remove = _change(
            "NASDAQ_100",
            "AAPL",
            CHANGE_REMOVE,
            date(2026, 9, 1),
            datetime(2026, 9, 1, 18, 0, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="AAPL",
            target_day=TARGET_DAY,
            baselines=BASELINES,
            changes=(remove,),
            coverage=_COVERED,
        )
        assert verdict.status == MEMBER

    def test_changes_for_unknown_index_ignored(self) -> None:
        change = _change(
            "SP500",
            "TSLA",
            CHANGE_ADD,
            date(2026, 9, 1),
            datetime(2026, 9, 1, 18, 0, tzinfo=_ET),
        )
        verdict = evaluate_membership(
            symbol="TSLA",
            target_day=TARGET_DAY,
            baselines=BASELINES,
            changes=(change,),
            coverage=_COVERED,
        )
        assert verdict.status == NOT_MEMBER
