# Strategy v2 P2.1 Evidence Design

## Goal

Make the forward-only shadow run auditable before any strategy promotion is
considered. P2.1 remains read-only with respect to the broker and real order
ledger: it cannot submit orders or enable live execution.

## Evidence contract

- Persist one immutable parameter snapshot for every strategy config hash.
- Allow decisions, virtual trades, and evaluation to be queried by symbol and
  explicit config version.
- Report collection progress against 20 observed trading days and 50 closed
  virtual trades. The only states are `COLLECTING` and `READY_FOR_REVIEW`;
  readiness never promotes or executes the strategy.
- Expose daily bar/trade/PnL evidence and data-quality warnings.
- Mark live comparison fields unavailable until a real version-aligned
  comparison source exists. Never serialize fabricated zero values as results.

## Safety and compatibility

The existing P0 engine, risk controller, order ledger, and broker gateway are
unchanged. The new version table is created with `checkfirst=True`; current
configs are backfilled lazily. Enabling or disabling collection is operational
state and does not create a strategy version.

## Acceptance

- Changing a tunable creates a new version and resets forward collection.
- Old versions remain queryable after a config change.
- Empty and weekend samples return valid `COLLECTING` evidence.
- Disabled collection reports `DISABLED` even when an old state row exists.
- API and UI never imply that a shadow/live comparison exists.
