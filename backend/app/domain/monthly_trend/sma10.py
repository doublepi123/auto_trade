"""Frozen rule SPY_MONTHLY_SMA10_CASH_V1 — pure computation.

Governance contract: ``app/domain/SPY_MONTHLY_SMA10_PREREGISTRATION.md``.

Purity contract (``app/domain/AGENTS.md``): this module imports nothing
beyond the standard library.  No DB, no network, no settings, no wall
clock — the replay CLI injects every input, including "now".

Frozen decisions implemented here (contract §3–§6):

- Signal: after the close of the last complete trading day of month m,
  ``I_m = 1{T_m > mean(T_{m-9..m})}`` where T is a total-return index
  built ONLY from information known by that day.  Equality means cash.
- Execution: a 0->1 transition buys at the NEXT trading day's open; 1->0
  sells everything at the next trading day's open; no trade when the
  signal is unchanged.  No add-ons, no DRIP, no shorts, no leverage, no
  intraday stop, no time exit.
- Sleeve: 5,000 USD initial cash; integer shares only; per-entry caps
  (<=100 shares, entry notional <=5,000 USD, cash+fees); dividends
  credited as cash on the pay date (fallback ex-date when the pay date
  is unknown — registered decision, contract §4.3) and never
  reinvested; idle cash earns 0; no trimming above the cap.
- Costs: commission ``1.568 + 0.0000641 x fill notional`` USD per order
  (the §9.8 SEC constants of ``app/core/accounting_fees.py``; this pure
  layer re-declares the two numbers as Decimal and the pin test asserts
  numeric equality with the core constants, so drift is impossible),
  base slippage 5 bps per side, stress 15 bps per side, the final
  liquidation charged in both cost scenarios.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Sequence

# ------------------------------------------------------------ frozen numbers

#: T_{m-9..m}: the month itself plus the 9 prior month-ends.
SIGNAL_LOOKBACK_MONTHS = 10
SMA10_WINDOW = 10
INITIAL_CASH_USD = Decimal("5000")
MAX_SHARES_PER_ENTRY = 100
MAX_ENTRY_NOTIONAL_USD = Decimal("5000")

#: Commission constants, numerically identical to
#: ``app.core.accounting_fees.SEC98_FIXED_USD`` / ``SEC98_NOTIONAL_RATE``.
#: Duplicated as Decimal literals because this pure layer keeps zero
#: imports beyond the stdlib; ``test_commission_constants_match_accounting_fees``
#: asserts equality so the two can never drift silently.
COMMISSION_FIXED_USD = Decimal("1.568")
COMMISSION_NOTIONAL_RATE = Decimal("0.0000641")

BASE_SLIPPAGE_BPS = 5.0
STRESS_SLIPPAGE_BPS = 15.0

#: Dividend-withholding sensitivity (disclosure only, never gating).
DIVIDEND_WITHHOLDING_RATE = 0.30

VERDICT_CORROBORATES_RISK_MANAGEMENT_VALUE = (
    "CORROBORATES_RISK_MANAGEMENT_VALUE"
)
VERDICT_DOES_NOT_CORROBORATE = "DOES_NOT_CORROBORATE"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"
VERDICT_INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
VERDICT_DATA_BLOCKED = "DATA_BLOCKED"

#: Primary window: 2012-01..2021-12 (120 complete months); 2010-06..2011-12
#: is warm-up only (the SMA needs 10 month-ends, first available for the
#: 2011-03 month-end).
PRIMARY_WINDOW_START_MONTH = (2012, 1)
PRIMARY_WINDOW_END_MONTH = (2021, 12)
REQUIRED_MONTHS = 120
MIN_CASH_MONTHS = 12
MIN_INVESTED_MONTHS = 60

#: Claim thresholds (one-sided 95% lower bounds must clear these).
CLAIM1_THRESHOLD = 0.0  # E[rS] > 0
CLAIM2_THRESHOLD = -0.001  # E[rS - rB] > -0.001
CLAIM3_THRESHOLD = 0.0  # E[0.8*min(rB,0)^2 - min(rS,0)^2] > 0
CLAIM3_DOWNSIDE_REDUCTION = 0.8
CLAIM4_THRESHOLD = 0.0  # stress-cost net monthly mean > 0

#: Split-anomaly detector only (contract 14.7 item 7): a 3:2 split
#: plus a +1% move escapes any pure price screen, so this screen NEVER
#: proves the absence of a split.  It stays as an anomaly detector; the
#: PROOF of "no split in window" is the sealed independent
#: ``splits.json`` (registered evidence), not this screen.
SPLIT_ANOMALY_FACTOR_HIGH = 1.5
SPLIT_ANOMALY_FACTOR_LOW = 2.0 / 3.0


class MonthlyTrendError(RuntimeError):
    """Malformed input for the frozen rule (fail-closed)."""


# ------------------------------------------------------------------- signal


@dataclass(frozen=True)
class MonthEndBar:
    """One month-end observation of the signal index.

    ``as_of`` is the last complete trading day of the calendar month;
    ``index_level`` is the total-return index T built only from
    information known by that day.
    """

    year: int
    month: int
    as_of: date
    index_level: float


def previous_calendar_month(key: tuple[int, int]) -> tuple[int, int]:
    year, month = key
    if month == 1:
        return (year - 1, 12)
    return (year, month - 1)


def next_calendar_month(key: tuple[int, int]) -> tuple[int, int]:
    year, month = key
    if month == 12:
        return (year + 1, 1)
    return (year, month + 1)


def consecutive_months(
    keys: Sequence[tuple[int, int]],
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """(first, last) when keys are consecutive calendar months with no
    gap and no duplicate; otherwise None."""

    if not keys:
        return None
    previous = keys[0]
    for current in keys[1:]:
        if next_calendar_month(previous) != current:
            return None
        previous = current
    return (keys[0], keys[-1])


def month_end_series(
    bars: Sequence[MonthEndBar],
) -> list[MonthEndBar]:
    """Sorted, validated month-end series (exactly one per month)."""

    ordered = sorted(
        bars, key=lambda bar: (bar.year, bar.month)
    )
    seen: set[tuple[int, int]] = set()
    for bar in ordered:
        key = (bar.year, bar.month)
        if key in seen:
            raise MonthlyTrendError(
                f"duplicate month-end observation for "
                f"{key[0]:04d}-{key[1]:02d}"
            )
        seen.add(key)
        if not math.isfinite(bar.index_level) or bar.index_level <= 0:
            raise MonthlyTrendError(
                "month-end index level must be finite and positive: "
                f"{key[0]:04d}-{key[1]:02d}"
            )
    return ordered


def sma10_signal(
    month_ends: Sequence[MonthEndBar],
    *,
    year: int,
    month: int,
) -> int | None:
    """``I_m = 1{T_m > mean(T_{m-9..m})}`` for the month (year, month).

    Returns None when fewer than 10 month-ends (the month itself plus the
    9 before it) are known — a warm-up state, never a signal.  Equality
    means cash (0).  Only information known at that month-end enters:
    the caller builds T from ex-date dividends (see
    ``build_signal_index``), so a dividend whose ex-date falls after the
    month-end can never move ``I_m``.
    """

    key = (year, month)
    levels: dict[tuple[int, int], float] = {
        (bar.year, bar.month): bar.index_level for bar in month_ends
    }
    window_keys = [key]
    cursor = key
    for _ in range(SMA10_WINDOW - 1):
        cursor = previous_calendar_month(cursor)
        window_keys.append(cursor)
    if any(k not in levels for k in window_keys):
        return None
    current = levels[key]
    average = math.fsum(levels[k] for k in window_keys) / SMA10_WINDOW
    return 1 if current > average else 0


@dataclass(frozen=True)
class DailyPriceRow:
    """RAW (NoAdjust) daily bar — executable prices for the account."""

    session: date
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class CorporateAction:
    """One sealed corporate action (dividend, split, or both).

    ``cash_amount`` is per-share gross; ``ratio`` is new/old shares
    (2.0 = a 2-for-1 split).  ``pay_date`` may be None when the source
    does not know it — the sleeve then credits on the ex-date
    (registered decision, contract §4.3).
    """

    symbol: str
    ex_date: date
    cash_amount: float | None
    ratio: float | None
    pay_date: date | None = None

    @property
    def credit_date(self) -> date:
        return self.pay_date if self.pay_date is not None else self.ex_date


def build_signal_index(
    daily_rows: Sequence[DailyPriceRow],
    actions: Sequence[CorporateAction],
) -> dict[date, float]:
    """Total-return index T for the SIGNAL only, day by day.

    Dividends are reinvested into the index on the EX-date (point-in-time
    total-return convention: the ex-date close already dropped by the
    dividend, so the credit is known that day).  Splits are neutralised
    by multiplying the daily price ratio by the split ratio, so a split
    never moves T.  The index starts at 1.0 on the first day and NEVER
    feeds the sleeve: shares, notionals and fees use RAW prices.
    """

    if not daily_rows:
        return {}
    ordered = sorted(daily_rows, key=lambda row: row.session)
    first_session = ordered[0].session
    div_by_ex_date: dict[date, float] = {}
    split_by_ex_date: dict[date, float] = {}
    # Decision 14.9 item 1: dividends dated BEFORE the first sealed bar
    # never update the index (they predate the account and the signal
    # window; reinvesting them would fabricate a level jump).
    for action in actions:
        if action.ex_date < first_session:
            continue
        if action.cash_amount is not None and action.cash_amount > 0:
            div_by_ex_date[action.ex_date] = (
                div_by_ex_date.get(action.ex_date, 0.0)
                + action.cash_amount
            )
        if (
            action.ratio is not None
            and action.ratio > 0
            and action.ratio != 1.0
        ):
            split_by_ex_date[action.ex_date] = action.ratio
    index: dict[date, float] = {}
    level = 1.0
    previous_close: float | None = None
    for row in ordered:
        if previous_close is not None:
            factor = row.close / previous_close
            dividend = div_by_ex_date.get(row.session)
            if dividend is not None:
                factor *= 1.0 + dividend / row.close
            ratio = split_by_ex_date.get(row.session)
            if ratio is not None:
                # RAW price already dropped by ~1/ratio; a split must not
                # move the total-return index.
                factor *= ratio
            level *= factor
        index[row.session] = level
        previous_close = row.close
    return index


def month_end_sessions(
    sessions: Sequence[date],
) -> dict[tuple[int, int], date]:
    """The last sealed trading day of each calendar month."""

    by_month: dict[tuple[int, int], date] = {}
    for session in sorted(sessions):
        by_month[(session.year, session.month)] = session
    return by_month


def month_end_index_levels(
    signal_index: dict[date, float],
    sessions: Sequence[date],
) -> list[MonthEndBar]:
    """Month-end T levels over the sealed sessions."""

    bars: list[MonthEndBar] = []
    for (year, month), as_of in sorted(
        month_end_sessions(sessions).items()
    ):
        level = signal_index.get(as_of)
        if level is None:
            raise MonthlyTrendError(
                "signal index is missing the month-end session "
                f"{as_of.isoformat()}"
            )
        bars.append(
            MonthEndBar(
                year=year,
                month=month,
                as_of=as_of,
                index_level=float(level),
            )
        )
    return bars


# --------------------------------------------------------------- execution


@dataclass(frozen=True)
class TradeFill:
    session: date
    side: str  # "BUY" | "SELL"
    shares: int
    raw_price: float
    slippage_bps: float
    commission_usd: Decimal
    fill_price: float  # raw_price * (1 +/- slippage)

    @property
    def notional_usd(self) -> Decimal:
        return Decimal(str(self.shares)) * Decimal(str(self.fill_price))


@dataclass(frozen=True)
class DividendCredit:
    session: date  # first sealed trading day >= credit date
    cash_amount_usd: Decimal


@dataclass(frozen=True)
class DividendReceivable:
    """A dividend entitled at the ex-date open, not yet paid.

    Included in NAV; NEVER spendable (item 1).  ``per_share_net`` is
    the per-share amount net of any withholding sensitivity rate.
    """

    ex_date: date
    amount_usd: Decimal


@dataclass
class SleeveState:
    """Mutable account state for one sleeve/benchmark leg."""

    cash: Decimal
    shares: int = 0
    fills: list[TradeFill] = field(default_factory=list)
    dividends: list[DividendCredit] = field(default_factory=list)
    receivables: list[DividendReceivable] = field(default_factory=list)

    def market_value(self, price: float) -> Decimal:
        """Cash + shares x price; receivables are NOT included here
        (callers add them explicitly for NAV marks)."""

        return self.cash + Decimal(str(self.shares)) * Decimal(str(price))

    def nav(self, price: float) -> Decimal:
        """Full NAV including unsettled dividend receivables."""

        return self.market_value(price) + sum(
            (receivable.amount_usd for receivable in self.receivables),
            Decimal("0"),
        )


def commission_usd(
    shares: int,
    fill_price: float,
    *,
    fixed: Decimal = COMMISSION_FIXED_USD,
    rate: Decimal = COMMISSION_NOTIONAL_RATE,
) -> Decimal:
    notional = Decimal(str(shares)) * Decimal(str(fill_price))
    return fixed + rate * notional


def entry_share_cap(
    *,
    cash_available: Decimal,
    raw_price: float,
    slippage_bps: float = BASE_SLIPPAGE_BPS,
    commission_fixed: Decimal = COMMISSION_FIXED_USD,
    commission_rate: Decimal = COMMISSION_NOTIONAL_RATE,
    max_shares: int = MAX_SHARES_PER_ENTRY,
    max_notional_usd: Decimal = MAX_ENTRY_NOTIONAL_USD,
) -> int:
    """Max integer shares buyable within cash+fees and both caps.

    Decision 14.7 item 4: the 5,000 NOTIONAL CAP compares ``shares x
    EXECUTABLE price`` (open plus slippage) - the exposure actually
    bought - NOT the raw price.  Commission remains a SEPARATE cash
    constraint (the cash test is cost + fee <= cash), so fees never
    consume notional headroom.  The fee is recomputed at each candidate
    q (it has a fixed component, so affordability is not linear in q).
    Exact integer search; no approximation.
    """

    if raw_price <= 0 or not math.isfinite(raw_price):
        return 0
    executable = raw_price * (1.0 + slippage_bps / 10_000.0)
    best = 0
    for candidate in range(1, max_shares + 1):
        cost = Decimal(str(candidate)) * Decimal(str(executable))
        fee = commission_fixed + commission_rate * cost
        if cost + fee > cash_available:
            break
        if cost > max_notional_usd:
            break
        best = candidate
    return best


def apply_fill(
    state: SleeveState,
    *,
    session: date,
    side: str,
    shares: int,
    raw_price: float,
    slippage_bps: float,
) -> TradeFill:
    if shares <= 0:
        raise MonthlyTrendError("shares must be positive")
    if raw_price <= 0 or not math.isfinite(raw_price):
        raise MonthlyTrendError("fill price must be finite and positive")
    direction = 1.0 if side == "BUY" else -1.0
    fill_price = raw_price * (1.0 + direction * slippage_bps / 10_000.0)
    fee = commission_usd(shares, fill_price)
    notional = Decimal(str(shares)) * Decimal(str(fill_price))
    if side == "BUY":
        if notional + fee > state.cash + Decimal("0.000001"):
            raise MonthlyTrendError(
                "entry notional + fee exceeds available cash"
            )
        state.cash -= notional + fee
        state.shares += shares
    elif side == "SELL":
        if shares > state.shares:
            raise MonthlyTrendError("sell exceeds held shares")
        state.cash += notional - fee
        state.shares -= shares
    else:
        raise MonthlyTrendError(f"unknown side: {side}")
    fill = TradeFill(
        session=session,
        side=side,
        shares=shares,
        raw_price=raw_price,
        slippage_bps=slippage_bps,
        commission_usd=fee,
        fill_price=fill_price,
    )
    state.fills.append(fill)
    return fill


def dividend_cash_credit(
    *,
    shares: int,
    cash_amount_per_share: float,
    withholding_rate: float = 0.0,
) -> Decimal:
    """Dividend credited as cash (never reinvested); 0 when flat."""

    if shares <= 0 or cash_amount_per_share <= 0:
        return Decimal("0")
    gross = Decimal(str(shares)) * Decimal(str(cash_amount_per_share))
    if withholding_rate:
        return gross * (Decimal("1") - Decimal(str(withholding_rate)))
    return gross


def apply_split(
    state: SleeveState,
    *,
    ex_date: date,
    ratio: float,
) -> int:
    """Adjust the share count for a split (cash untouched)."""

    if ratio <= 0:
        raise MonthlyTrendError("split ratio must be positive")
    new_shares = state.shares * ratio
    if new_shares != int(new_shares):
        raise MonthlyTrendError(
            "fractional shares after split on "
            f"{ex_date.isoformat()}; sealed split data is inconsistent "
            "with integer-share accounting"
        )
    state.shares = int(new_shares)
    return state.shares


# ------------------------------------------------------------- simulation


def next_session_after(
    sessions: Sequence[date], target: date
) -> date | None:
    """The first sealed trading day strictly after ``target``."""

    for session in sorted(sessions):
        if session > target:
            return session
    return None


@dataclass(frozen=True)
class MonthRecord:
    year: int
    month: int
    month_end_session: date
    invested: bool  # held shares at the month-end mark
    equity: Decimal  # cash + shares x close + receivables (full NAV)


@dataclass(frozen=True)
class SleeveResult:
    records: list[MonthRecord]
    fills: list[TradeFill]
    dividends: list[DividendCredit]
    final_liquidation: TradeFill | None
    terminal_cash_after_liquidation: Decimal
    outstanding_receivables: tuple[DividendReceivable, ...] = ()


def simulate_leg(
    *,
    sessions: Sequence[date],
    daily_rows: Sequence[DailyPriceRow],
    actions: Sequence[CorporateAction],
    scoring_months: Sequence[tuple[int, int]],
    entry_events: Sequence[tuple[date, str]],  # (session, "BUY"|"SELL")
    slippage_bps: float,
    initial_cash: Decimal = INITIAL_CASH_USD,
    liquidation_session: date | None = None,
    max_shares: int = MAX_SHARES_PER_ENTRY,
    max_notional_usd: Decimal = MAX_ENTRY_NOTIONAL_USD,
    dividends_withholding_rate: float = 0.0,
) -> SleeveResult:
    """Simulate one account leg (sleeve or buy-and-hold benchmark).

    Decision 14.7 items 1-2 (dividend entitlement + liquidation):

    - Dividend ENTITLEMENT is fixed at the EX-DATE, BEFORE that
      session's open trade: the shares carried INTO the ex-date session
      (i.e. held at the previous close) create a RECEIVABLE that is
      included in NAV but never spendable.  On the pay session (first
      sealed session >= pay date; ex-date fallback when unknown) the
      receivable moves to cash whether or not the position is still
      held.  A buy AT the ex-date open therefore earns nothing; a sell
      at the ex-date open keeps the full entitlement.
    - The FINAL LIQUIDATION is processed IN TIME ORDER at its own
      session's open, and no NEW position may be opened on or after the
      liquidation session (the window never carries a fresh position
      past its end; a last-month BUY->SELL round trip is impossible).

    Order of events within one session: split (ex-date) -> dividend
    entitlement (ex-date, pre-open) -> open trade (scheduled event or
    the liquidation) -> dividend settlement (pay session) -> month-end
    mark.  Month-end equity is the FULL NAV: cash + shares x close +
    outstanding receivables.  Idle cash earns 0; a position above the
    cap is never trimmed.
    """

    by_session = {row.session: row for row in daily_rows}
    sealed = sorted(set(sessions))
    state = SleeveState(cash=initial_cash)
    events_by_session: dict[date, str] = dict(entry_events)
    split_by_ex_date: dict[date, float] = {}
    ex_amounts: dict[date, list[float]] = {}
    # (pay_session, ex_date) settlement schedule, one entry per action.
    settlements: list[tuple[date, date]] = []
    for action in actions:
        if (
            action.ratio is not None
            and action.ratio > 0
            and action.ratio != 1.0
        ):
            split_by_ex_date[action.ex_date] = action.ratio
        if action.cash_amount is not None and action.cash_amount > 0:
            ex_amounts.setdefault(action.ex_date, []).append(
                action.cash_amount
            )
            pay_session = next(
                (s for s in sealed if s >= action.credit_date), None
            )
            if pay_session is not None:
                settlements.append((pay_session, action.ex_date))

    records: list[MonthRecord] = []
    end_by_month = month_end_sessions(sealed)
    scoring_set = set(scoring_months)
    final_liquidation: TradeFill | None = None
    # Receivables keyed by ex_date so settlement pairs exactly (an
    # action's entitlement settles with its own pay event).
    receivable_by_ex: dict[date, DividendReceivable] = {}

    for session in sealed:
        row = by_session.get(session)
        if row is None:
            raise MonthlyTrendError(
                f"sealed session has no RAW bar: {session.isoformat()}"
            )
        # 1. split on the ex-date, before anything else that day.
        ratio = split_by_ex_date.get(session)
        if ratio is not None:
            apply_split(state, ex_date=session, ratio=ratio)
        # 2. dividend entitlement at the ex-date open, BEFORE the open
        #    trade (item 1): shares carried in from yesterday.
        for amount in ex_amounts.get(session, ()):
            if state.shares <= 0:
                continue
            per_share_net = Decimal(str(amount)) * (
                Decimal("1") - Decimal(str(dividends_withholding_rate))
            )
            entitled = Decimal(str(state.shares)) * per_share_net
            receivable = DividendReceivable(
                ex_date=session, amount_usd=entitled
            )
            receivable_by_ex[session] = receivable
            state.receivables.append(receivable)
        # 3. the open trade: liquidation first (it IS the session's
        #    trade), else the scheduled event; no BUY on/after the
        #    liquidation session.
        is_liquidation_session = (
            liquidation_session is not None
            and session == liquidation_session
        )
        side = events_by_session.get(session)
        if is_liquidation_session and state.shares > 0:
            final_liquidation = apply_fill(
                state,
                session=session,
                side="SELL",
                shares=state.shares,
                raw_price=row.open,
                slippage_bps=slippage_bps,
            )
        elif side == "BUY" and state.shares == 0:
            may_open = liquidation_session is None or (
                session < liquidation_session
            )
            if may_open:
                shares = entry_share_cap(
                    cash_available=state.cash,
                    raw_price=row.open,
                    slippage_bps=slippage_bps,
                    max_shares=max_shares,
                    max_notional_usd=max_notional_usd,
                )
                if shares > 0:
                    apply_fill(
                        state,
                        session=session,
                        side="BUY",
                        shares=shares,
                        raw_price=row.open,
                        slippage_bps=slippage_bps,
                    )
        elif side == "SELL" and state.shares > 0:
            apply_fill(
                state,
                session=session,
                side="SELL",
                shares=state.shares,
                raw_price=row.open,
                slippage_bps=slippage_bps,
            )
        # 4. dividend settlement on the pay session: the receivable
        #    moves to cash whether or not the position is still held.
        for pay_session, ex_date in settlements:
            if pay_session != session:
                continue
            receivable = receivable_by_ex.pop(ex_date, None)
            if receivable is None:
                continue
            state.cash += receivable.amount_usd
            state.dividends.append(
                DividendCredit(
                    session=session,
                    cash_amount_usd=receivable.amount_usd,
                )
            )
            state.receivables.remove(receivable)
        # 5. month-end mark: FULL NAV including outstanding receivables.
        key = (session.year, session.month)
        if key in scoring_set and end_by_month.get(key) == session:
            records.append(
                MonthRecord(
                    year=key[0],
                    month=key[1],
                    month_end_session=session,
                    invested=state.shares > 0,
                    equity=state.nav(row.close),
                )
            )

    # Item 2: the LAST scored month includes the registered
    # next-open liquidation.  The liquidation session is the first
    # sealed session after the last month-end, so its proceeds belong
    # to that month's mark: rewrite the final record's equity to the
    # terminal cash once the liquidation has executed in time order
    # (settlements after the last month-end - e.g. a dividend paying
    # between month-end and liquidation - also land in cash first).
    if (
        liquidation_session is not None
        and records
        and final_liquidation is not None
        and liquidation_session > records[-1].month_end_session
    ):
        last = records[-1]
        # Decision 14.9 item 3: final equity = liquidation CASH plus
        # any receivable still outstanding at the sealed end (its pay
        # date falls after the data): both parts belong to the final
        # month's NAV.
        outstanding = sum(
            (r.amount_usd for r in state.receivables), Decimal("0")
        )
        records[-1] = MonthRecord(
            year=last.year,
            month=last.month,
            month_end_session=last.month_end_session,
            invested=last.invested,
            equity=state.cash + outstanding,
        )

    return SleeveResult(
        records=records,
        fills=state.fills,
        dividends=state.dividends,
        final_liquidation=final_liquidation,
        terminal_cash_after_liquidation=state.cash,
        outstanding_receivables=tuple(state.receivables),
    )


def monthly_return_series(
    records: Sequence[MonthRecord],
    *,
    initial_equity: Decimal,
) -> list[float]:
    """Month-over-month equity returns including cash months.

    The first scoring month's return is measured against the initial
    cash (the leg starts flat at the window open — registered decision,
    contract §4.1: warm-up months never trade, so both legs mark 5000
    entering 2012-01).
    """

    returns: list[float] = []
    previous = float(initial_equity)
    for record in records:
        current = float(record.equity)
        if previous <= 0:
            raise MonthlyTrendError("non-positive equity in window")
        returns.append(current / previous - 1.0)
        previous = current
    return returns


def sleeve_entry_exit_events(
    *,
    signals: dict[tuple[int, int], int | None],
    scoring_months: Sequence[tuple[int, int]],
    sessions: Sequence[date],
) -> list[tuple[date, str]]:
    """Trade events from the signal series (the frozen execution rule).

    Decision 14.7 item 3 (warm-up boundary): the signal of the LAST
    WARM-UP month (2011-12) is the position state ENTERING the first
    scored session - if it differs from flat, the first scored session
    itself opens with the target trade at the open after the 2011-12
    month-end.  The caller supplies that boundary signal via the
    ``signals`` map (the CLI passes warm-up months too); a MISSING
    required signal - fewer than 10 consecutive month-ends by the
    boundary or inside the window - raises MonthlyReplayBlockedError
    (DATA_BLOCKED), NEVER a silent flat.

    For every month m with a known signal I_m, the transition from the
    previous known signal decides: 0->1 buys at the next sealed
    session's open after m's month-end; 1->0 sells everything there; no
    change trades nothing.  The state entering the window is the last
    warm-up signal (default 0 when the caller passes no warm-up keys,
    preserving the flat boundary for pure-window callers).
    """

    sealed = sorted(set(sessions))
    end_by_month = month_end_sessions(sealed)
    events: list[tuple[date, str]] = []
    scoring_set = list(scoring_months)
    # The boundary state: the signal of the month BEFORE the first
    # scored month, if the caller provided it; otherwise flat.
    warmup_keys = [
        key for key in signals if key < scoring_set[0]
    ]
    if warmup_keys:
        boundary_key = max(warmup_keys)
        boundary_signal = signals[boundary_key]
        if boundary_signal is None:
            raise MonthlyReplayBlockedError(
                "the boundary (last warm-up) month "
                f"{boundary_key[0]:04d}-{boundary_key[1]:02d} has no "
                "signal: fewer than 10 consecutive month-ends by then "
                "(DATA_BLOCKED, never a silent flat)"
            )
        previous_signal = boundary_signal
        # The boundary trade executes at the open after the boundary
        # month-end = the first scored session's open.
        if boundary_signal != 0:
            month_end = end_by_month.get(boundary_key)
            if month_end is None:
                raise MonthlyReplayBlockedError(
                    "no month-end session for the boundary month "
                    f"{boundary_key[0]:04d}-{boundary_key[1]:02d}"
                )
            execution = next_session_after(sealed, month_end)
            if execution is None:
                raise MonthlyReplayBlockedError(
                    "no next sealed session after the boundary month-end "
                    f"{month_end.isoformat()} (missing next open)"
                )
            events.append(
                (execution, "BUY" if boundary_signal == 1 else "SELL")
            )
    for key in scoring_set:
        signal = signals.get(key)
        if signal is None:
            # A required signal inside the window is missing: DATA_BLOCKED.
            raise MonthlyReplayBlockedError(
                f"scoring month {key[0]:04d}-{key[1]:02d} has no signal "
                "(fewer than 10 consecutive month-ends; DATA_BLOCKED, "
                "never a silent flat)"
            )
        if signal != previous_signal:
            month_end = end_by_month.get(key)
            if month_end is None:
                raise MonthlyTrendError(
                    f"no month-end session for {key[0]:04d}-{key[1]:02d}"
                )
            execution = next_session_after(sealed, month_end)
            if execution is None:
                raise MonthlyReplayBlockedError(
                    "no next sealed session after the month-end "
                    f"{month_end.isoformat()} (missing next open)"
                )
            events.append(
                (execution, "BUY" if signal == 1 else "SELL")
            )
            previous_signal = signal
    return events


class MonthlyReplayBlockedError(MonthlyTrendError):
    """A DATA_BLOCKED condition: missing month-end / next open /
    uncertain corporate-action data.  Never fill in or drop a month."""


def validate_window_data(
    *,
    sessions: Sequence[date],
    scoring_months: Sequence[tuple[int, int]],
    daily_rows: Sequence[DailyPriceRow],
    actions: Sequence[CorporateAction],
    calendar_sessions: Sequence[date] | None = None,
    symbol: str = "SPY.US",
    protected_opens: Sequence[date] | None = None,
) -> None:
    """Raise MonthlyReplayBlockedError for every DATA_BLOCKED shape.

    Decision 14.7 items 5-6:

    - RAW OHLC must be finite and > 0 with
      ``low <= min(open, close) <= max(open, close) <= high``;
    - no DUPLICATE bar dates;
    - ``pay_date >= ex_date`` on every corporate action (a pay date
      before the ex-date is uncertain data);
    - month-ends and next opens are validated against the sealed
      calendar; when an INDEPENDENT ``calendar_sessions`` (the sealed
      NYSE-rule calendar, item 5) is supplied, a sealed session missing
      from it - or an independent session with no SPY bar - is a
      DATA_BLOCKED gap (dates missing on BOTH symbols are caught this
      way, which the SPY n QQQ intersection alone cannot do);
    - a scoring month with no sealed session (missing month-end);
    - a month-end whose next sealed session is missing (missing next
      open) — checked for the LAST scoring month too;
    - a corporate action with neither a cash amount nor a ratio, or a
      non-positive/non-finite value where one is present.

    Decision 14.11 (single-point OHLC relation exemption): the uniform
    gate is enforced per RELATION.  The one registered anomaly
    (SPY.US 2020-11-18, ``open > high`` - a non-execution day whose
    open is outside the read set) is excused while every other check
    still passes.  The bar is never skipped; its close stays checked;
    the exemption never asserts any field is correct.  Additionally,
    every PROTECTED open (a potential execution day, whether or not a
    trade happens - ``protected_opens``) must lie within
    ``[low, high]`` independent of the ordering relations.
    """

    sealed = sorted(set(sessions))
    end_by_month = month_end_sessions(sealed)
    rows_by_session: dict[date, DailyPriceRow] = {}
    for row in daily_rows:
        if row.session in rows_by_session:
            raise MonthlyReplayBlockedError(
                f"duplicate RAW bar date for {symbol}: "
                + row.session.isoformat()
            )
        rows_by_session[row.session] = row
        values = (row.open, row.high, row.low, row.close)
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise MonthlyReplayBlockedError(
                f"RAW OHLC not finite-positive for {symbol} on "
                f"{row.session.isoformat()}"
            )
        # Decision 14.11: the uniform OHLC gate is enforced per
        # RELATION.  A violated relation is excused ONLY when it is the
        # single registered anomaly (SPY.US 2020-11-18, open > high) -
        # a session whose open is outside the read set.  Every other
        # relation on every other session still blocks, and the
        # registered bar's CLOSE stays fully checked (it is read).
        for relation in _ohlc_relation_violations(row):
            if _is_registered_anomaly(
                symbol, row.session, relation
            ):
                continue
            raise MonthlyReplayBlockedError(
                f"RAW OHLC violates low <= min(o,c) <= max(o,c) <= "
                f"high for {symbol} on {row.session.isoformat()} "
                f"({relation})"
            )
    # Decision 14.11: every PROTECTED open (a potential execution day,
    # whether or not a trade happens) must lie within [low, high] even
    # when the registered relation exemption applies elsewhere on the
    # bar.  This is field-level: it reads the open DIRECTLY.
    for protected in sorted(set(protected_opens or ())):
        row = rows_by_session.get(protected)
        if row is None:
            continue  # missing bars are caught by the calendar checks
        if not (row.low <= row.open <= row.high):
            raise MonthlyReplayBlockedError(
                f"protected execution-day open outside [low, high] for "
                f"{symbol} on {protected.isoformat()}"
            )
    independent = (
        set(calendar_sessions) if calendar_sessions is not None else None
    )
    for session in sealed:
        row = rows_by_session.get(session)
        if row is None:
            raise MonthlyReplayBlockedError(
                f"missing RAW daily bar on sealed session "
                f"{session.isoformat()}"
            )
        if independent is not None and session not in independent:
            raise MonthlyReplayBlockedError(
                "sealed session absent from the independent calendar: "
                f"{session.isoformat()}"
            )
    if independent is not None:
        required = [
            session
            for session in sorted(independent)
            if _calendar_required(
                session, scoring_months, sealed, end_by_month
            )
        ]
        for session in required:
            if session not in rows_by_session:
                raise MonthlyReplayBlockedError(
                    "independent calendar session has no SPY bar: "
                    f"{session.isoformat()}"
                )
    for key in scoring_months:
        month_end = end_by_month.get(key)
        if month_end is None:
            raise MonthlyReplayBlockedError(
                f"missing month-end session for "
                f"{key[0]:04d}-{key[1]:02d}"
            )
        if next_session_after(sealed, month_end) is None:
            raise MonthlyReplayBlockedError(
                f"missing next open after the month-end "
                f"{month_end.isoformat()}"
            )
    for action in actions:
        cash = action.cash_amount
        ratio = action.ratio
        if cash is None and ratio is None:
            raise MonthlyReplayBlockedError(
                "uncertain corporate action (neither cash nor ratio) on "
                f"{action.ex_date.isoformat()}"
            )
        if cash is not None and (not math.isfinite(cash) or cash <= 0):
            raise MonthlyReplayBlockedError(
                f"invalid dividend amount on {action.ex_date.isoformat()}"
            )
        if ratio is not None and (not math.isfinite(ratio) or ratio <= 0):
            raise MonthlyReplayBlockedError(
                f"invalid split ratio on {action.ex_date.isoformat()}"
            )
        if action.pay_date is not None and action.pay_date < action.ex_date:
            raise MonthlyReplayBlockedError(
                "pay date before ex date (uncertain data) on "
                f"{action.ex_date.isoformat()}"
            )


# ------------------------------------------------- registered anomaly (14.11)


#: Decision 14.11 (SPY_MONTHLY_SMA10_PREREGISTRATION.md): the SINGLE
#: registered OHLC relation anomaly.  SPY.US 2020-11-18 carries
#: ``open > high`` in the sealed RAW cache (the provider reproduces the
#: bar identically); that session's open is OUTSIDE the read set (it is
#: not the next sealed session after any month-end 2011-12..2021-12),
#: so the ``open > high`` relation alone - on that symbol, that
#: session, with every other check still passing - is exempt from the
#: uniform OHLC gate.  The exemption NEVER asserts that open, high or
#: any other field is correct, never skips the bar, and never excuses a
#: read field (the bar's close is still read and still checked).
REGISTERED_OHLC_ANOMALIES: tuple[dict[str, str], ...] = (
    {
        "symbol": "SPY.US",
        "session": "2020-11-18",
        "relation": "open > high",
    },
)

OHLC_RELATION_OPEN_ABOVE_HIGH = "open > high"
OHLC_RELATION_LOW_ABOVE_CLOSE = "low > close"


def _ohlc_relation_violations(row: DailyPriceRow) -> list[str]:
    """The OHLC ordering relations this row violates (finite-positive
    already checked), in the registered 14.11 vocabulary: relations,
    never fields.  Unregistered shapes (``open < low``, ``close >
    high``) are reported verbatim so they can never match a registered
    exemption."""

    violated: list[str] = []
    if row.open > row.high:
        violated.append(OHLC_RELATION_OPEN_ABOVE_HIGH)
    if row.low > row.close:
        violated.append(OHLC_RELATION_LOW_ABOVE_CLOSE)
    if row.open < row.low:
        violated.append("open < low")
    if row.close > row.high:
        violated.append("close > high")
    return violated


def _is_registered_anomaly(
    symbol: str, session: date, relation: str
) -> bool:
    return any(
        entry["symbol"] == symbol
        and entry["session"] == session.isoformat()
        and entry["relation"] == relation
        for entry in REGISTERED_OHLC_ANOMALIES
    )


def detect_ohlc_anomalies(
    daily_rows: Sequence[DailyPriceRow],
    *,
    symbol: str = "SPY.US",
) -> list[dict[str, str]]:
    """Re-derive the full violation set from bars: every
    ``(symbol, session, relation)`` whose OHLC ordering is violated.
    This is the re-derivation primitive evaluate compares against the
    sealed ledger (decision 14.11: the derived set must EQUAL the
    ledger exactly)."""

    anomalies: list[dict[str, str]] = []
    for row in daily_rows:
        for relation in _ohlc_relation_violations(row):
            anomalies.append(
                {
                    "symbol": symbol,
                    "session": row.session.isoformat(),
                    "relation": relation,
                }
            )
    return sorted(
        anomalies,
        key=lambda entry: (
            entry["symbol"], entry["session"], entry["relation"]
        ),
    )


def _calendar_required(
    session: date,
    scoring_months: Sequence[tuple[int, int]],
    sealed: Sequence[date],
    end_by_month: dict[tuple[int, int], date],
) -> bool:
    """Is this independent-calendar session REQUIRED to have a bar?

    Decision 14.9 item 4: the requirement covers the FULL registered
    span.  Warm-up starts at the FIRST INDEPENDENT-CALENDAR session
    (not min(sealed)); the end is the next sealed session after the
    last scored month-end (not "month-end + 10 days").  A "1 bar per
    month" cache fails this, as does any head/tail truncation.
    """

    if not sealed:
        return False
    last_key = max(scoring_months) if scoring_months else None
    if last_key is None:
        return False
    last_month_end = end_by_month.get(last_key)
    final_open = (
        next_session_after(sealed, last_month_end)
        if last_month_end is not None
        else None
    )
    if final_open is not None and session > final_open:
        return False
    # Everything from the first sealed session through the final open
    # is required (the independent calendar governs the head).
    return session >= min(sealed)


def screen_unexplained_split_anomalies(
    daily_rows: Sequence[DailyPriceRow],
    actions: Sequence[CorporateAction],
    *,
    sessions: Sequence[date] | None = None,
    factor_high: float = SPLIT_ANOMALY_FACTOR_HIGH,
    factor_low: float = SPLIT_ANOMALY_FACTOR_LOW,
) -> list[tuple[date, float]]:
    """ANOMALY DETECTOR ONLY (contract 14.7 item 7) - never a proof.

    Derives implied factors from RAW adjacent closes: ``factor =
    prev_close / close``; any factor ``>= factor_high`` or ``<=
    factor_low`` that does NOT coincide with a registered split action
    on that session is reported as an anomaly.  A 3:2 split plus a +1%
    move (factor ~1.485) ESCAPES this screen - which is exactly why the
    screen may never be used as proof: the sealed independent
    ``splits.json`` evidence is the proof, this is only a tripwire.

    Returns the offending ``(session, implied_factor)`` pairs (empty =
    no anomaly detected).  ``sessions`` optionally restricts to the
    sealed calendar (rows outside it are ignored, e.g. QQQ-only dates).
    """

    sealed = set(sessions) if sessions is not None else None
    split_sessions = {
        action.ex_date
        for action in actions
        if action.ratio is not None and action.ratio != 1.0
    }
    ordered = sorted(daily_rows, key=lambda row: row.session)
    offenders: list[tuple[date, float]] = []
    previous: DailyPriceRow | None = None
    for row in ordered:
        if sealed is not None and row.session not in sealed:
            previous = None
            continue
        if previous is not None:
            factor = previous.close / row.close
            if (
                factor >= factor_high or factor <= factor_low
            ) and row.session not in split_sessions:
                offenders.append((row.session, factor))
        previous = row
    return offenders


# ----------------------------------------------------------------- verdicts


@dataclass(frozen=True)
class BootstrapConfig:
    block_length: int = 12
    resamples: int = 10_000
    seed: int = 20260928


#: Registered bootstrap: circular moving-block, block 12, 10,000
#: resamples, seed 20260928 (contract §6.1).
BOOTSTRAP_CONFIG = BootstrapConfig()


@dataclass(frozen=True)
class ClaimBounds:
    mean: float
    lower: float  # one-sided 95% percentile lower bound
    upper: float  # one-sided 95% percentile upper bound


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    position = q * (len(sorted_values) - 1)
    lower_index = int(math.floor(position))
    upper_index = int(math.ceil(position))
    if lower_index == upper_index:
        return sorted_values[lower_index]
    weight = position - lower_index
    return (
        sorted_values[lower_index] * (1.0 - weight)
        + sorted_values[upper_index] * weight
    )


def is_constant(values: Sequence[float]) -> bool:
    return bool(values) and all(value == values[0] for value in values)


def circular_block_bootstrap_bounds(
    values: Sequence[float],
    *,
    config: BootstrapConfig,
) -> ClaimBounds:
    """One-sided 95% percentile bounds, circular moving-block bootstrap.

    Deterministic: a LOCAL ``random.Random(config.seed)`` — never the
    global module state.  Each resample concatenates ceil(n / block)
    circular blocks; the statistic is the resample mean; the lower bound
    is the 5th percentile of the resampled means and the upper bound the
    95th percentile.

    Registered degenerate policy (contract §6.2): a constant series
    yields constant resampled means, so the bounds collapse onto the
    mean; the claim is then UNDECIDABLE and the overall verdict stays
    INCONCLUSIVE — a constant series can neither corroborate a direction
    nor refute one.  Never an error, never a pass.

    Pairing (contract §6.1): every claim series is month-aligned and
    bootstrapped with the SAME seed and block length, so the block start
    indices are identical across claims — the paired-month structure is
    preserved by construction (pinned by test).
    """

    if not values:
        raise MonthlyTrendError("bootstrap needs at least one observation")
    n = len(values)
    block = config.block_length
    if block <= 0 or block > n:
        raise MonthlyTrendError(
            f"block length {block} invalid for {n} observations"
        )
    rng = random.Random(config.seed)
    blocks_per_resample = math.ceil(n / block)
    resampled_length = blocks_per_resample * block
    means: list[float] = []
    for _ in range(config.resamples):
        total = 0.0
        for _ in range(blocks_per_resample):
            start = rng.randrange(n)
            for offset in range(block):
                total += values[(start + offset) % n]
        means.append(total / resampled_length)
    means.sort()
    return ClaimBounds(
        mean=math.fsum(values) / n,
        lower=_percentile(means, 0.05),
        upper=_percentile(means, 0.95),
    )


@dataclass(frozen=True)
class ClaimEvaluations:
    claim1_positive_expectancy: ClaimBounds  # E[rS]
    claim2_giveup: ClaimBounds  # E[rS - rB]
    claim3_downside: ClaimBounds  # E[0.8*min(rB,0)^2 - min(rS,0)^2]
    claim4_stress_positive: ClaimBounds  # E[rS_stress]
    degenerate: bool
    #: Which claim INPUT SERIES are constant (item 11): subset of
    #: {"claim1","claim2","claim3","claim4"}.
    degenerate_series: tuple[str, ...] = ()


def downside_series(
    sleeve: Sequence[float], benchmark: Sequence[float]
) -> list[float]:
    if len(sleeve) != len(benchmark):
        raise MonthlyTrendError("sleeve/benchmark series must be paired")
    return [
        CLAIM3_DOWNSIDE_REDUCTION * min(r_b, 0.0) ** 2
        - min(r_s, 0.0) ** 2
        for r_s, r_b in zip(sleeve, benchmark)
    ]


def evaluate_claims(
    *,
    sleeve_monthly_returns: Sequence[float],
    benchmark_monthly_returns: Sequence[float],
    stress_sleeve_monthly_returns: Sequence[float],
    config: BootstrapConfig = BOOTSTRAP_CONFIG,
) -> ClaimEvaluations:
    if not (
        len(sleeve_monthly_returns)
        == len(benchmark_monthly_returns)
        == len(stress_sleeve_monthly_returns)
    ):
        raise MonthlyTrendError(
            "sleeve, benchmark and stress series must be paired"
        )
    sleeve = list(sleeve_monthly_returns)
    benchmark = list(benchmark_monthly_returns)
    stress = list(stress_sleeve_monthly_returns)
    give_up = [a - b for a, b in zip(sleeve, benchmark)]
    downside = downside_series(sleeve, benchmark)
    degenerate_series = tuple(
        name
        for name, series in (
            ("claim1", sleeve),
            ("claim2", give_up),
            ("claim3", downside),
            ("claim4", stress),
        )
        if is_constant(series)
    )
    degenerate = bool(degenerate_series)
    return ClaimEvaluations(
        claim1_positive_expectancy=circular_block_bootstrap_bounds(
            sleeve, config=config
        ),
        claim2_giveup=circular_block_bootstrap_bounds(
            give_up, config=config
        ),
        claim3_downside=circular_block_bootstrap_bounds(
            downside, config=config
        ),
        claim4_stress_positive=circular_block_bootstrap_bounds(
            stress, config=config
        ),
        degenerate=degenerate,
        degenerate_series=degenerate_series,
    )


@dataclass(frozen=True)
class SampleGates:
    months: int
    cash_months: int
    invested_months: int

    @property
    def sufficient(self) -> bool:
        return (
            self.months >= REQUIRED_MONTHS
            and self.cash_months >= MIN_CASH_MONTHS
            and self.invested_months >= MIN_INVESTED_MONTHS
        )

    def failures(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.months < REQUIRED_MONTHS:
            reasons.append(f"MONTHS({self.months}<{REQUIRED_MONTHS})")
        if self.cash_months < MIN_CASH_MONTHS:
            reasons.append(
                f"CASH_MONTHS({self.cash_months}<{MIN_CASH_MONTHS})"
            )
        if self.invested_months < MIN_INVESTED_MONTHS:
            reasons.append(
                f"INVESTED_MONTHS({self.invested_months}"
                f"<{MIN_INVESTED_MONTHS})"
            )
        return tuple(reasons)


def sample_gates_from_records(
    records: Sequence[MonthRecord],
) -> SampleGates:
    invested = sum(1 for record in records if record.invested)
    return SampleGates(
        months=len(records),
        cash_months=len(records) - invested,
        invested_months=invested,
    )


#: (claim name, bounds getter, threshold) in claim order.
_CLAIM_THRESHOLDS: tuple[tuple[str, float], ...] = (
    ("claim1_positive_expectancy", CLAIM1_THRESHOLD),
    ("claim2_giveup", CLAIM2_THRESHOLD),
    ("claim3_downside", CLAIM3_THRESHOLD),
    ("claim4_stress_positive", CLAIM4_THRESHOLD),
)


def decide_verdict(
    *,
    gates: SampleGates,
    claims: ClaimEvaluations,
) -> str:
    """Frozen verdict mapping (contract §6.3).

    DATA_BLOCKED and INSUFFICIENT_DATA are decided by the caller from
    input-level evidence; this maps sample gates + claim bounds only.

    - ``CORROBORATES_RISK_MANAGEMENT_VALUE`` iff the sample is sufficient
      AND every claim's one-sided 95% LOWER bound clears its threshold
      (#1 > 0, #2 > -0.001, #3 > 0, #4 > 0).
    - ``DOES_NOT_CORROBORATE`` iff the sample is sufficient AND at least
      one claim's one-sided UPPER bound is <= its threshold.
    - ``INCONCLUSIVE`` otherwise (insufficient sample, degenerate
      constant series, or bounds that straddle).
    """

    if not gates.sufficient:
        return VERDICT_INCONCLUSIVE
    values = (
        claims.claim1_positive_expectancy,
        claims.claim2_giveup,
        claims.claim3_downside,
        claims.claim4_stress_positive,
    )
    # Item 11: the degeneracy check is PER CLAIM INPUT SERIES (rS,
    # rS-rB, the downside term, stress rS).  A constant series cannot
    # pass its claim (lower bound == mean, never > threshold) and can
    # never refute it either; the verdict for a constant claim input is
    # INCONCLUSIVE.  ``degenerate_series`` flags which claims are
    # constant, so the reason is reportable.
    if claims.degenerate_series:
        return VERDICT_INCONCLUSIVE
    for bounds, (_name, threshold) in zip(values, _CLAIM_THRESHOLDS):
        if bounds.upper <= threshold:
            return VERDICT_DOES_NOT_CORROBORATE
    for bounds, (_name, threshold) in zip(values, _CLAIM_THRESHOLDS):
        if not bounds.lower > threshold:
            return VERDICT_INCONCLUSIVE
    return VERDICT_CORROBORATES_RISK_MANAGEMENT_VALUE


# --------------------------------------------------------------- descriptive


def descriptive_statistics(
    *,
    sleeve_monthly_returns: Sequence[float],
    qqq_monthly_returns: Sequence[float],
) -> dict[str, object]:
    """Descriptive-only slice (never gating, contract §7)."""

    sleeve = list(sleeve_monthly_returns)
    qqq = list(qqq_monthly_returns)
    # Item 12: the equity curve is COMPOUNDED (two -10% months -> 19%
    # drawdown, not 20%).
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    losing_streak = 0
    longest_losing_streak = 0
    for value in sleeve:
        equity *= 1.0 + value
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, 1.0 - equity / peak)
        if value < 0:
            losing_streak += 1
            longest_losing_streak = max(longest_losing_streak, losing_streak)
        else:
            losing_streak = 0
    wins = sum(1 for value in sleeve if value > 0)
    rolling_12m: list[float] = []
    for start in range(0, len(sleeve) - 11):
        window = sleeve[start : start + 12]
        compounded = 1.0
        for value in window:
            compounded *= 1.0 + value
        rolling_12m.append(compounded - 1.0)
    return {
        "descriptive_only": True,
        "never_gating": True,
        "monthly_win_rate": (wins / len(sleeve)) if sleeve else None,
        "worst_month": min(sleeve) if sleeve else None,
        "max_drawdown": max_drawdown,
        "longest_losing_streak": longest_losing_streak,
        "rolling_12m_returns": rolling_12m,
        "qqq_differences": [a - b for a, b in zip(sleeve, qqq)],
        "statement": (
            "stable means long-run positive expectancy with acceptable "
            "drawdown, NOT profit every month; an 8%/year hypothetical "
            "on 5,000 is about 33 USD per month; 120 months may well be "
            "INCONCLUSIVE (0.5%/month at 3% monthly volatility needs "
            "~223 months for 80% power)"
        ),
    }
