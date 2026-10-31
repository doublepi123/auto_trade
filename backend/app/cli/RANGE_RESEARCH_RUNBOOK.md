# Offline range research

This driver never imports trading settings, a broker or a database and never
submits orders, changes a live risk state, or promotes a strategy. The trading
runner does not import it. All timestamps and files below are research evidence.

The active research phase is `crossday`. The previous entry-reclaim candidate
is frozen without adoption after its initial retrospective diagnostic. Do not
accumulate multiple forward hypotheses concurrently.

## Register before collecting evidence

From `backend/`, using the deployed research image's Python or the validated
Python 3.11 environment:

```bash
python -m app.cli.range_replay register --phase crossday \
  --output /evidence/crossday-forward-registration.json
```

Keep that file. Registration creates it exclusively and never overwrites it.
It binds both protocols, parameters, calendar, fee model, tape reader, replay,
statistics, driver and CLI source hashes. The actual registration timestamp
sets the evidence clock; the earliest possible opening is November 2, 2026,
14:30 UTC. Registration after an opening starts at the next full session.

Changing any bound file or parameter requires a new registration and resets
the clock. A hash mismatch is a refusal, not permission to edit the old file.
Do not copy forward trade evidence to a new identity or backdate a registration.

## Evaluate only completed sessions

```bash
python -m app.cli.range_replay evaluate --phase crossday \
  --tape-root /tapes --start 2026-11-02 --end 2026-11-02 \
  --registration /evidence/crossday-forward-registration.json \
  --output /evidence/crossday-through-20261102.json
```

The end date is the latest completed US exchange date. Run at least five
minutes after the actual session close. Market calendars determine DST,
holidays and shortened sessions. An early forward invocation returns
`WAITING_FOR_MARKET_DATA`, with no invented performance figures. Never run
two jobs into the same output; prior reports are immutable.

The market recorder continues using its existing recording configuration.
Supply its host data directory as a **read-only** tape mount. The evaluator
reads only RTH-intersecting UTC hour files and exhausts each selected file,
verifying schema, clocks, ordering, gzip integrity and compressed-file digest.
It does not repair or sort corrupt files. Partial or corrupt inputs refuse the
report. Missing sessions stay visible at zero coverage.

Use a separate, pinned research image as a one-shot container with no network,
read-only root, no capabilities, no credentials, no database mount, and bounded
CPU/memory. Override the image's default web entry point with this Python CLI.
Only its evidence directory should be writable. This deploys the research
capability without restarting the healthy trading service.

## Read results

- `replays`: every filled trade with causal decision/fill identities, unresolved
  inventory, missed fills, quality rejection counters, rolling drawdown and
  loss-guard state.
- `arms`: complete per-day gross, fees, extra costs, all three net columns,
  zero-trade days, missing/poor-coverage days and CR1 per-trade intervals.
- `paired_days`: candidate minus baseline per scheduled session, including
  zero-trade sessions. This is not a matched subset of old trades.
- `split`: fixed whole-day early 2/3 and late 1/3 diagnostic slices; replay state
  continues across them. Historical slices are never an unseen holdout.
- `evidence`, `implementation`, `tape`: registration identity, producer source
  hashes and stable compressed-input manifest.

Incomplete quality, unresolved inventory, fewer than 20 independent close days
or fewer than 30 complete round trips remain `INSUFFICIENT_DATA`. Even complete
evidence can only become `REVIEW_REQUIRED`; no output authorizes adoption.
Existing long-sample/selection-bias/human-review governance and P0 locks apply.

## Recovery and rollback

After an interrupted evaluation, inspect the original process/container before
starting another. A missing output is not proof that the job stopped. Retain
the command, process/container handle, log and terminal exit status.

On evaluator failure, retain the original logs and inputs. Repair only a proven
defect, run the repository gates, publish a new pinned research image, and
register a new forward clock if semantics changed. Never resume, force-resume,
restart or otherwise mutate the trading runtime to fix this offline job.

Rollback is stopping the research job/schedule and using the previous pinned
research image and its compatible registration. No trading-state or database
rollback is needed.
