# Completion receipt — opening-momentum-top10-pit-historical-v3

Registered retrospective validation of the frozen forward rule
`INDEX_CATALOG_STOCKS_IN_PLAY_ORB_TOP10_CHALLENGER`
(config_version `44e3c377d37ca27f921eef983a985f8e45667c283d72ddb056aafbe0d026def9`).
Contract: `backend/app/domain/OPENING_MOMENTUM_HISTORICAL_REPLAY.md` (§6 step 5).

## Provenance

| Item | Value |
|---|---|
| Plan registered (first, before any data) | `61e96b12` |
| Final evaluate code (clean tree) | `db1d2afd2d2f888ad87752e5c7902241310cc7a0` |
| Attempt | 1 of 1 (`attempts/0001.claim`), no rerun |
| Seal manifest sha256 | `8186ec271aaa166f28adde0997f6d026db984777c1c8e032cc63f32791380c63` (294 files, sealed 2026-09-28T07:33:29Z) |
| Original plan sha256 (v2, imported) | `aaed92f33c7ca03859b32b331cb419c7cfb7a2e1f63162b314145729ad80ac35` |
| Bound plan sha256 | `cc7e0c0484b083830c73cb58a960b0969344c3dd2879509bb7b0129d287e1415` |
| Output sha256 | `19d52179983331f6a6bd814051e017d829e40ebf576560e3bfd7948c5cf223b9` |
| Fetch | 2026-09-27 12:31Z → 2026-09-28 06:09Z, 31,694 requests, 147 COMPLETE + 2 PERMANENT_FAILURE (SGEN, SPLK: 301600) |

Before this single run, the contract was changed twice, both times before any outcome existed:
change decision 1 (trading-day source) and change decision 2 (evaluate path, v3).
No price file was opened and no outcome was computed before the run.

## Verdict: `DOES_NOT_CORROBORATE`

| Statistic | Value |
|---|---|
| Trades n / weeks W | 419 / 140 (minimums 125 / 26) |
| Mean net at 30 bps | −28.6 bps per trade |
| Week-clustered SE | 7.24 bps |
| Bounds at 30 bps, L30 / U30 | −40.6 / −16.6 bps |
| Bounds at 50 bps stress, L50 / U50 | −60.6 / −36.6 bps |
| U30 < 0 | **true**: base-cost net expectancy is significantly negative, not only under stress |

## Gates (all passed)

| Gate | Value | Threshold |
|---|---|---|
| (a) auditable sessions | 97.75 % | ≥ 95 % |
| (b) member-days missing | 1.01 % | ≤ 2 % |
| (b) still-listed missing member-days | 0 | = 0 |
| (c) unresolved exit sessions | 0 | = 0 |

## Descriptive only (never gating)

- Mean gross return +1.4 bps per trade; win rate 39.6 %; exits: 323 fixed-hold, 96 stop.
- Every calendar half-year is negative: 2023-H2 −48.2, 2024-H1 −11.7, 2024-H2 −37.0, 2025-H1 −21.4, 2025-H2 −29.6, 2026-H1 −27.9 (mean net bps per trade).
- 6 of 32 months have a positive equal-notional net sum.

## What this result authorises (§3 / §8 of the contract)

- It only feeds research priority and possibly the material for a written abandonment decision.
- It does NOT change the forward preregistration (E = 2026-09-28, 125 trades, gates), does NOT add trades to the forward cohort, does NOT authorise orders, and is NOT a negative futility trigger for the forward test. Stopping the forward test requires a separate written decision.
