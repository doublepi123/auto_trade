"""Guidance-raise eligibility boundaries (PREREGISTRATION §10.3)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.domain.guidance_continuation.eligibility import (
    GUIDANCE_KIND,
    WITHDRAWN_KIND,
    GuidanceStatement,
    announcement_window,
    evaluate_guidance_raise,
    previous_trading_day_close,
)
from app.domain.guidance_continuation.config import DEFAULT_GUIDANCE_CONFIG

_ET = ZoneInfo("America/New_York")
_UTC = ZoneInfo("UTC")

# A regular Tuesday with no holiday around it.
TARGET_DAY = date(2026, 9, 22)
PREV_DAY = date(2026, 9, 21)


def _et(y: int, m: int, d: int, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=_ET)


def _utc_dt(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=_UTC)


def _stmt(
    *,
    low: str = "100",
    high: str = "110",
    kind: str = GUIDANCE_KIND,
    published: datetime | None = None,
    observed: datetime | None = None,
    reviewed: datetime | None = None,
    registered: datetime | None = None,
    symbol: str = "AAPL",
    fy: int = 2026,
    metric: str = "TOTAL_REVENUE",
    currency: str = "USD",
) -> GuidanceStatement:
    pub = published or _et(2026, 9, 21, 16, 30)
    obs = observed or _et(2026, 9, 22, 8, 0)
    return GuidanceStatement(
        symbol=symbol,
        fiscal_year=fy,
        metric=metric,
        currency=currency,
        low=Decimal(low),
        high=Decimal(high),
        kind=kind,
        source_published_at=pub,
        first_observed_at=obs,
        # Default the two extra evidence timestamps to the same instant as
        # first_observed_at so tests that only care about one deadline are
        # not accidentally late; override per-case.
        transcription_reviewed_at=reviewed or obs,
        registered_at=registered or obs,
        source_sha256="0" * 64,
    )


def _base_prior(published: datetime) -> GuidanceStatement:
    """A normal prior: midpoint 105, observed well before the deadline."""
    return _stmt(
        low="100",
        high="110",
        published=published,
        observed=published + timedelta(hours=2),
    )


def _new_raise(published: datetime, observed: datetime | None = None) -> GuidanceStatement:
    """Midpoint 107.1+ → raise of exactly 2.00% when prior is [100,110]."""
    return _stmt(low="102", high="112.2", published=published, observed=observed)


class TestAnnouncementWindow:
    def test_previous_trading_day_close_regular_day(self) -> None:
        close = previous_trading_day_close(TARGET_DAY)
        assert close == _et(2026, 9, 21, 16, 0)

    def test_previous_close_skips_weekend(self) -> None:
        # Monday 2026-09-21 → previous trading day is Friday 2026-09-18.
        assert previous_trading_day_close(date(2026, 9, 21)) == _et(
            2026, 9, 18, 16, 0
        )

    def test_previous_close_skips_holiday_half_day(self) -> None:
        # 2026-11-27 is a 13:00 half day (Black Friday); the trading day
        # before Mon 2026-11-30 is that half day closing 13:00 ET.
        assert previous_trading_day_close(date(2026, 11, 30)) == _et(
            2026, 11, 27, 13, 0
        )

    def test_previous_close_skips_observed_holiday(self) -> None:
        # 2026-07-03 is a full US closure (Independence Day observed);
        # before Mon 2026-07-06 the previous trading day is Thu 2026-07-02.
        assert previous_trading_day_close(date(2026, 7, 6)) == _et(
            2026, 7, 2, 16, 0
        )

    def test_announcement_window_bounds(self) -> None:
        start, end = announcement_window(TARGET_DAY)
        assert start == _et(2026, 9, 21, 16, 0)
        assert end == _et(2026, 9, 22, 9, 0)

    def test_published_exactly_at_previous_close_is_out(self) -> None:
        # (previous close, 09:00]: the close instant itself is EXCLUSIVE.
        published = _et(2026, 9, 21, 16, 0)
        verdict = evaluate_guidance_raise(
            _new_raise(published),
            (_base_prior(_et(2026, 6, 1, 12, 0)),),
            target_day=TARGET_DAY,
        )
        assert not verdict.eligible
        assert verdict.reason_code == "OUTSIDE_ANNOUNCEMENT_WINDOW"

    def test_published_just_after_previous_close_is_in(self) -> None:
        published = _et(2026, 9, 21, 16, 0, 1)
        verdict = evaluate_guidance_raise(
            _new_raise(published),
            (_base_prior(_et(2026, 6, 1, 12, 0)),),
            target_day=TARGET_DAY,
        )
        assert verdict.eligible

    def test_published_exactly_at_0900_is_in(self) -> None:
        published = _et(2026, 9, 22, 9, 0)
        verdict = evaluate_guidance_raise(
            _new_raise(published),
            (_base_prior(_et(2026, 6, 1, 12, 0)),),
            target_day=TARGET_DAY,
        )
        assert verdict.eligible

    def test_published_after_0900_is_out(self) -> None:
        published = _et(2026, 9, 22, 9, 0, 1)
        verdict = evaluate_guidance_raise(
            _new_raise(published),
            (_base_prior(_et(2026, 6, 1, 12, 0)),),
            target_day=TARGET_DAY,
        )
        assert not verdict.eligible
        assert verdict.reason_code == "OUTSIDE_ANNOUNCEMENT_WINDOW"


class TestRegistrationDeadline:
    def test_observed_at_092459_is_in(self) -> None:
        verdict = evaluate_guidance_raise(
            _new_raise(
                _et(2026, 9, 22, 7, 0), observed=_et(2026, 9, 22, 9, 24, 59)
            ),
            (_base_prior(_et(2026, 6, 1, 12, 0)),),
            target_day=TARGET_DAY,
        )
        assert verdict.eligible

    def test_observed_at_092500_is_out(self) -> None:
        verdict = evaluate_guidance_raise(
            _new_raise(
                _et(2026, 9, 22, 7, 0), observed=_et(2026, 9, 22, 9, 25, 0)
            ),
            (_base_prior(_et(2026, 6, 1, 12, 0)),),
            target_day=TARGET_DAY,
        )
        assert not verdict.eligible
        assert verdict.reason_code == "LATE_REGISTRATION"


class TestPriorSelection:
    def _new(self) -> GuidanceStatement:
        return _new_raise(_et(2026, 9, 21, 17, 0))

    def test_b1_new_withdrawn_statement_is_not_eligible(self) -> None:
        # §10.3 L475: the event requires guidance for the same fiscal year;
        # a WITHDRAWN new "statement" with normal-looking values is not a
        # guidance raise at all.  Values are raise-shaped ([102, 112.2]
        # over [100, 110] = exactly +2%) so ONLY the kind check rejects it
        # — the pre-fix code judged exactly this ELIGIBLE.
        withdrawn_new = _stmt(
            kind=WITHDRAWN_KIND,
            low="102",
            high="112.2",
            published=_et(2026, 9, 21, 17, 0),
        )
        verdict = evaluate_guidance_raise(
            withdrawn_new,
            (_base_prior(_et(2026, 6, 1, 12, 0)),),
            target_day=TARGET_DAY,
        )
        assert not verdict.eligible
        assert verdict.reason_code == "INVALID_KIND"

    def test_b1_new_with_unknown_kind_is_not_eligible(self) -> None:
        # Same raise-shaped values with a kind outside the vocabulary.
        weird = _stmt(
            kind="SOMETHING_ELSE",
            low="102",
            high="112.2",
            published=_et(2026, 9, 21, 17, 0),
        )
        verdict = evaluate_guidance_raise(
            weird,
            (_base_prior(_et(2026, 6, 1, 12, 0)),),
            target_day=TARGET_DAY,
        )
        assert not verdict.eligible
        assert verdict.reason_code == "INVALID_KIND"

    def test_b1_prior_negative_low_is_not_eligible(self) -> None:
        # prior [-1, 110]: non-positive low cannot serve as a prior.
        bad_prior = _stmt(
            low="-1", high="110", published=_et(2026, 6, 1, 12, 0)
        )
        verdict = evaluate_guidance_raise(
            self._new(), (bad_prior,), target_day=TARGET_DAY
        )
        assert not verdict.eligible

    def test_b1_prior_low_above_high_is_not_eligible(self) -> None:
        # prior [100, 90]: low > high cannot serve as a prior.
        bad_prior = _stmt(
            low="100", high="90", published=_et(2026, 6, 1, 12, 0)
        )
        verdict = evaluate_guidance_raise(
            self._new(), (bad_prior,), target_day=TARGET_DAY
        )
        assert not verdict.eligible

    def test_b1_prior_with_unknown_kind_is_not_eligible(self) -> None:
        bad_prior = _stmt(
            kind="REITERATION", published=_et(2026, 6, 1, 12, 0)
        )
        verdict = evaluate_guidance_raise(
            self._new(), (bad_prior,), target_day=TARGET_DAY
        )
        assert not verdict.eligible

    def test_b1_conflicting_priors_at_same_instant_not_eligible(self) -> None:
        # Two DIFFERENT prior records published at the SAME instant: which
        # one is "the most recent" is undefined → ineligible with a reason.
        a = _base_prior(_et(2026, 6, 1, 12, 0))  # [100, 110]
        b = GuidanceStatement(
            symbol="AAPL",
            fiscal_year=2026,
            metric="TOTAL_REVENUE",
            currency="USD",
            low=Decimal("200"),
            high=Decimal("210"),
            kind=GUIDANCE_KIND,
            source_published_at=_et(2026, 6, 1, 12, 0),
            first_observed_at=_et(2026, 6, 1, 14, 0),
            transcription_reviewed_at=_et(2026, 6, 1, 14, 0),
            registered_at=_et(2026, 6, 1, 14, 0),
            source_sha256="1" * 64,  # distinct content → genuine conflict
        )
        verdict = evaluate_guidance_raise(
            self._new(), (a, b), target_day=TARGET_DAY
        )
        assert not verdict.eligible
        assert verdict.reason_code == "CONFLICTING_RECORDS"

    def test_prior_at_exactly_120_days_is_in(self) -> None:
        # 2026-09-21 17:00 ET minus 120 days = 2026-05-24 (Sunday).
        # published at exactly the boundary is "within 120 calendar days".
        new = _new_raise(_et(2026, 9, 21, 17, 0))
        prior_pub = _et(2026, 5, 24, 17, 0)
        assert (new.source_published_at - prior_pub).days == 120
        verdict = evaluate_guidance_raise(
            new,
            (_base_prior(prior_pub),),
            target_day=TARGET_DAY,
        )
        assert verdict.eligible

    def test_prior_just_beyond_120_days_is_out(self) -> None:
        # Local-calendar semantics: 2026-05-23 → 2026-09-21 is 121 natural
        # ET days, outside the 120-day lookback regardless of clock time.
        prior_pub = _et(2026, 5, 23, 16, 59)
        assert (
            _et(2026, 9, 21, 17, 0).date() - prior_pub.date()
        ).days == 121
        verdict = evaluate_guidance_raise(
            self._new(),
            (_base_prior(prior_pub),),
            target_day=TARGET_DAY,
        )
        assert not verdict.eligible
        assert verdict.reason_code == "NO_PRIOR_GUIDANCE"

    def test_b7_cross_dst_exactly_120_local_days_is_in(self) -> None:
        # Prior 2026-07-06 08:00 ET, new 2026-11-03 08:00 ET: exactly 120
        # America/New_York LOCAL calendar days apart (both UTC-04:00 →
        # UTC-05:00 transitions crossed), yet 120×24h arithmetic in UTC
        # says 120 days + 1 h → NO_PRIOR_GUIDANCE.  Natural days must be
        # counted on the ET local calendar, inclusive of the boundary.
        prior_pub = _et(2026, 7, 6, 8, 0)
        new_pub = _et(2026, 11, 3, 8, 0)
        # sanity: local-date difference is exactly 120 days.
        assert (new_pub.date() - prior_pub.date()).days == 120
        # sanity: 120×24h UTC arithmetic EXCLUDES the prior (the old bug).
        assert (
            new_pub.astimezone(_UTC) - prior_pub.astimezone(_UTC)
            > timedelta(days=120)
        )
        verdict = evaluate_guidance_raise(
            _stmt(low="102", high="112.2", published=new_pub),
            (_base_prior(prior_pub),),
            target_day=date(2026, 11, 3),
        )
        assert verdict.eligible

    def test_b7_cross_dst_121_local_days_is_out(self) -> None:
        # 121 local calendar days → outside the 120-day lookback.
        prior_pub = _et(2026, 7, 5, 8, 0)
        new_pub = _et(2026, 11, 3, 8, 0)
        assert (new_pub.date() - prior_pub.date()).days == 121
        verdict = evaluate_guidance_raise(
            _stmt(low="102", high="112.2", published=new_pub),
            (_base_prior(prior_pub),),
            target_day=date(2026, 11, 3),
        )
        assert not verdict.eligible
        assert verdict.reason_code == "NO_PRIOR_GUIDANCE"

    def test_most_recent_prior_is_used_not_an_earlier_one(self) -> None:
        earlier_lower = _base_prior(_et(2026, 6, 1, 12, 0))  # [100,110]
        recent = _stmt(
            low="102", high="112", published=_et(2026, 7, 1, 12, 0)
        )  # midpoint 107
        # new midpoint must clear +2% over 107, not over 105.
        new = _stmt(low="103", high="113.98", published=_et(2026, 9, 21, 17, 0))
        verdict = evaluate_guidance_raise(
            new, (earlier_lower, recent), target_day=TARGET_DAY
        )
        # (103+113.98)/(102+112) - 1 = 216.98/214 - 1 = 1.3925...% < 2%
        assert not verdict.eligible
        assert verdict.reason_code == "INSUFFICIENT_RAISE"

    def test_no_prior_at_all(self) -> None:
        verdict = evaluate_guidance_raise(
            self._new(), (), target_day=TARGET_DAY
        )
        assert not verdict.eligible
        assert verdict.reason_code == "NO_PRIOR_GUIDANCE"

    def test_withdrawn_as_most_recent_prior_is_out(self) -> None:
        withdrawn = _stmt(
            kind=WITHDRAWN_KIND,
            low="0",
            high="0",
            published=_et(2026, 7, 1, 12, 0),
        )
        verdict = evaluate_guidance_raise(
            self._new(),
            (withdrawn, _base_prior(_et(2026, 6, 1, 12, 0))),
            target_day=TARGET_DAY,
        )
        assert not verdict.eligible
        assert verdict.reason_code == "PRIOR_WITHDRAWN"

    def test_prior_visible_only_if_observed_before_deadline(self) -> None:
        # Prior observed at 09:30 ET on the target day is NOT visible.
        # B2: the latest prior blocks without fallback — the reason is
        # PRIOR_HISTORY_INCOMPLETE, not NO_PRIOR_GUIDANCE (no fallback).
        late_prior = _stmt(
            low="100",
            high="110",
            published=_et(2026, 6, 1, 12, 0),
            observed=_et(2026, 9, 22, 9, 30),
        )
        verdict = evaluate_guidance_raise(
            self._new(), (late_prior,), target_day=TARGET_DAY
        )
        assert not verdict.eligible
        assert verdict.reason_code == "PRIOR_HISTORY_INCOMPLETE"

    def test_prior_of_different_fiscal_year_is_ignored(self) -> None:
        other_fy = GuidanceStatement(
            symbol="AAPL",
            fiscal_year=2025,
            metric="TOTAL_REVENUE",
            currency="USD",
            low=Decimal("100"),
            high=Decimal("110"),
            kind=GUIDANCE_KIND,
            source_published_at=_et(2026, 6, 1, 12, 0),
            first_observed_at=_et(2026, 6, 1, 14, 0),
            transcription_reviewed_at=_et(2026, 6, 1, 14, 0),
            registered_at=_et(2026, 6, 1, 14, 0),
            source_sha256="0" * 64,
        )
        verdict = evaluate_guidance_raise(
            _new_raise(_et(2026, 9, 21, 17, 0)),
            (other_fy,),
            target_day=TARGET_DAY,
        )
        assert not verdict.eligible
        assert verdict.reason_code == "NO_PRIOR_GUIDANCE"

    def test_b2_newer_prior_registered_after_cutoff_blocks_fallback(
        self,
    ) -> None:
        # June prior [100,110] visible; a NEWER September prior [200,210]
        # registered AFTER the target day's 09:25 cutoff.  The prior must
        # be the most recent statement published before the new one —
        # including not-yet-visible ones — so falling back to June and
        # declaring a raise is FORBIDDEN.  INELIGIBLE, no fallback.
        june = _base_prior(_et(2026, 6, 1, 12, 0))
        sept = _stmt(
            low="200",
            high="210",
            published=_et(2026, 9, 10, 12, 0),
            observed=_et(2026, 9, 22, 9, 30),  # registered after cutoff
        )
        new = _stmt(low="102", high="112.2", published=_et(2026, 9, 21, 17, 0))
        verdict = evaluate_guidance_raise(
            new, (june, sept), target_day=TARGET_DAY
        )
        assert not verdict.eligible
        assert verdict.reason_code != "NO_PRIOR_GUIDANCE"

    def test_b2_incomparable_newest_prior_blocks_fallback(self) -> None:
        # The newest prior is a different METRIC: the event is
        # INELIGIBLE (PRIOR_NOT_COMPARABLE), never falling back to the
        # older same-metric prior.
        old_same = _base_prior(_et(2026, 6, 1, 12, 0))
        newer_other_metric = _stmt(
            metric="EPS",
            low="10",
            high="11",
            published=_et(2026, 9, 10, 12, 0),
        )
        new = _stmt(low="102", high="112.2", published=_et(2026, 9, 21, 17, 0))
        verdict = evaluate_guidance_raise(
            new, (old_same, newer_other_metric), target_day=TARGET_DAY
        )
        assert not verdict.eligible
        assert verdict.reason_code == "PRIOR_NOT_COMPARABLE"

    def test_b2_incomparable_newest_prior_by_currency_blocks_fallback(
        self,
    ) -> None:
        old_same = _base_prior(_et(2026, 6, 1, 12, 0))
        newer_other_ccy = _stmt(
            currency="EUR",
            low="10",
            high="11",
            published=_et(2026, 9, 10, 12, 0),
        )
        new = _stmt(low="102", high="112.2", published=_et(2026, 9, 21, 17, 0))
        verdict = evaluate_guidance_raise(
            new, (old_same, newer_other_ccy), target_day=TARGET_DAY
        )
        assert not verdict.eligible
        assert verdict.reason_code == "PRIOR_NOT_COMPARABLE"

    def test_b2_invisible_newest_prior_same_metric_no_fallback(self) -> None:
        # Same-metric newest prior observed after the cutoff: still the
        # most recent statement, so it blocks; no fallback to June.
        june = _base_prior(_et(2026, 6, 1, 12, 0))
        sept_invisible = _stmt(
            low="200",
            high="210",
            published=_et(2026, 9, 10, 12, 0),
            observed=_et(2026, 9, 22, 9, 30),
        )
        new = _stmt(low="102", high="112.2", published=_et(2026, 9, 21, 17, 0))
        verdict = evaluate_guidance_raise(
            new, (june, sept_invisible), target_day=TARGET_DAY
        )
        assert not verdict.eligible
        assert verdict.reason_code == "PRIOR_HISTORY_INCOMPLETE"


class TestR1SameInstantConflict:
    """R1: statements tied as most-recent conflict on ANY decision-relevant
    difference — identical ``source_sha256`` does NOT mean an identical
    transcription.  Never pick by input order."""

    TARGET = date(2026, 11, 3)

    def _new(self) -> GuidanceStatement:
        return _stmt(
            low="102",
            high="112.2",
            published=_et(2026, 11, 2, 17, 0),
            observed=_et(2026, 11, 3, 8, 0),
        )

    def test_r1_same_hash_different_values_all_permutations(self) -> None:
        # Both priors share source_published_at AND source_sha256 but
        # transcribe different ranges ([100,110] vs [200,210]).
        lo = _stmt(
            low="100", high="110", published=_et(2026, 9, 10, 12, 0)
        )
        hi = _stmt(
            low="200", high="210", published=_et(2026, 9, 10, 12, 0)
        )
        for order in ((lo, hi), (hi, lo)):
            verdict = evaluate_guidance_raise(
                self._new(), order, target_day=self.TARGET
            )
            assert not verdict.eligible, f"order {order[0].low}-first"
            assert verdict.reason_code == "CONFLICTING_RECORDS"

    def test_r1_same_hash_different_kind_conflicts(self) -> None:
        guidance = _stmt(
            low="100", high="110", published=_et(2026, 9, 10, 12, 0)
        )
        withdrawn = _stmt(
            kind=WITHDRAWN_KIND,
            low="0",
            high="0",
            published=_et(2026, 9, 10, 12, 0),
        )
        for order in ((guidance, withdrawn), (withdrawn, guidance)):
            verdict = evaluate_guidance_raise(
                self._new(), order, target_day=self.TARGET
            )
            assert not verdict.eligible
            assert verdict.reason_code == "CONFLICTING_RECORDS"

    def test_r1_same_hash_different_visibility_conflicts(self) -> None:
        # Same hash and instant, but one transcription was obtained at
        # 08:00 and the other at 09:00 — a decision-relevant difference.
        early = _stmt(
            low="100",
            high="110",
            published=_et(2026, 9, 10, 12, 0),
            observed=_et(2026, 9, 10, 14, 0),
        )
        later = _stmt(
            low="100",
            high="110",
            published=_et(2026, 9, 10, 12, 0),
            observed=_et(2026, 9, 10, 15, 0),
        )
        for order in ((early, later), (later, early)):
            verdict = evaluate_guidance_raise(
                self._new(), order, target_day=self.TARGET
            )
            assert not verdict.eligible
            assert verdict.reason_code == "CONFLICTING_RECORDS"

    def test_r1_exact_duplicates_dedupe_and_remain_eligible(self) -> None:
        # Two EXACT copies (every decision-relevant field equal) are one
        # record: eligible, in any order, any multiplicity.
        dup = _stmt(low="100", high="110", published=_et(2026, 9, 10, 12, 0))
        exact = _stmt(low="100", high="110", published=_et(2026, 9, 10, 12, 0))
        for priors in ((dup, exact), (exact, dup), (dup, dup, exact)):
            verdict = evaluate_guidance_raise(
                self._new(), priors, target_day=self.TARGET
            )
            assert verdict.eligible
            assert verdict.reason_code == "ELIGIBLE"



class TestRaiseMath:
    def test_raise_exactly_at_2pct_is_eligible(self) -> None:
        # (102+112.2)/(100+110) - 1 = 214.2/210 - 1 = 0.02 exactly.
        new = _stmt(low="102", high="112.2", published=_et(2026, 9, 21, 17, 0))
        assert (new.low + new.high) / Decimal(210) - 1 == Decimal("0.02")
        verdict = evaluate_guidance_raise(
            new, (_base_prior(_et(2026, 6, 1, 12, 0)),), target_day=TARGET_DAY
        )
        assert verdict.eligible

    def test_raise_just_below_2pct_is_out(self) -> None:
        new = _stmt(low="102", high="112.19", published=_et(2026, 9, 21, 17, 0))
        verdict = evaluate_guidance_raise(
            new, (_base_prior(_et(2026, 6, 1, 12, 0)),), target_day=TARGET_DAY
        )
        assert not verdict.eligible
        assert verdict.reason_code == "INSUFFICIENT_RAISE"

    def test_lower_bound_violated_even_with_big_midpoint_raise(self) -> None:
        # L1 < L0 fails even though the midpoint rises > 2%.
        new = _stmt(low="99", high="120", published=_et(2026, 9, 21, 17, 0))
        verdict = evaluate_guidance_raise(
            new, (_base_prior(_et(2026, 6, 1, 12, 0)),), target_day=TARGET_DAY
        )
        assert not verdict.eligible
        assert verdict.reason_code == "LOWER_BOUND"

    def test_upper_bound_violated(self) -> None:
        new = _stmt(low="101", high="109", published=_et(2026, 9, 21, 17, 0))
        verdict = evaluate_guidance_raise(
            new, (_base_prior(_et(2026, 6, 1, 12, 0)),), target_day=TARGET_DAY
        )
        assert not verdict.eligible
        assert verdict.reason_code == "LOWER_BOUND"

    def test_single_point_guidance_low_eq_high(self) -> None:
        prior = _stmt(low="105", high="105", published=_et(2026, 6, 1, 12, 0))
        new = _stmt(low="108", high="108", published=_et(2026, 9, 21, 17, 0))
        verdict = evaluate_guidance_raise(
            new, (prior,), target_day=TARGET_DAY
        )
        assert verdict.eligible


class TestFieldMismatches:
    def test_currency_mismatch(self) -> None:
        new = _stmt(currency="EUR", published=_et(2026, 9, 21, 17, 0))
        verdict = evaluate_guidance_raise(
            new, (_base_prior(_et(2026, 6, 1, 12, 0)),), target_day=TARGET_DAY
        )
        assert not verdict.eligible
        assert verdict.reason_code == "CURRENCY_MISMATCH"

    def test_metric_mismatch(self) -> None:
        new = _stmt(metric="EPS", published=_et(2026, 9, 21, 17, 0))
        verdict = evaluate_guidance_raise(
            new, (_base_prior(_et(2026, 6, 1, 12, 0)),), target_day=TARGET_DAY
        )
        assert not verdict.eligible
        assert verdict.reason_code == "METRIC_MISMATCH"

    def test_non_positive_values(self) -> None:
        new = _stmt(low="0", high="110", published=_et(2026, 9, 21, 17, 0))
        verdict = evaluate_guidance_raise(
            new, (_base_prior(_et(2026, 6, 1, 12, 0)),), target_day=TARGET_DAY
        )
        assert not verdict.eligible
        assert verdict.reason_code == "INVALID_VALUES"

    def test_low_above_high(self) -> None:
        new = _stmt(low="120", high="110", published=_et(2026, 9, 21, 17, 0))
        verdict = evaluate_guidance_raise(
            new, (_base_prior(_et(2026, 6, 1, 12, 0)),), target_day=TARGET_DAY
        )
        assert not verdict.eligible
        assert verdict.reason_code == "INVALID_VALUES"

    def test_non_finite_values(self) -> None:
        new = GuidanceStatement(
            symbol="AAPL",
            fiscal_year=2026,
            metric="TOTAL_REVENUE",
            currency="USD",
            low=Decimal("Infinity"),
            high=Decimal("110"),
            kind=GUIDANCE_KIND,
            source_published_at=_et(2026, 9, 21, 17, 0),
            first_observed_at=_et(2026, 9, 22, 8, 0),
            transcription_reviewed_at=_et(2026, 9, 22, 8, 0),
            registered_at=_et(2026, 9, 22, 8, 0),
            source_sha256="0" * 64,
        )
        verdict = evaluate_guidance_raise(
            new, (_base_prior(_et(2026, 6, 1, 12, 0)),), target_day=TARGET_DAY
        )
        assert not verdict.eligible
        assert verdict.reason_code == "INVALID_VALUES"


class TestNaiveTimestamps:
    def test_naive_timestamp_is_rejected(self) -> None:
        new = GuidanceStatement(
            symbol="AAPL",
            fiscal_year=2026,
            metric="TOTAL_REVENUE",
            currency="USD",
            low=Decimal("102"),
            high=Decimal("112.2"),
            kind=GUIDANCE_KIND,
            source_published_at=datetime(2026, 9, 21, 17, 0),
            first_observed_at=_et(2026, 9, 22, 8, 0),
            transcription_reviewed_at=_et(2026, 9, 22, 8, 0),
            registered_at=_et(2026, 9, 22, 8, 0),
            source_sha256="0" * 64,
        )
        with pytest.raises(ValueError):
            evaluate_guidance_raise(
                new, (_base_prior(_et(2026, 6, 1, 12, 0)),), target_day=TARGET_DAY
            )
