# P89–P93 Review Detail Observability Design

## Goal

Improve the Review workspace so an operator can understand what happened on each review day without leaving the page. This is a frontend-only, read-only batch that derives extra context from existing `GET /api/review` response fields.

## Scope

| Iteration | Feature | Behavior |
|---|---|---|
| P89 | Day composition strip | Each day card shows counts for LLM interactions, orders, events, snapshots, and error tags. |
| P90 | Day state badges | Each day card shows derived badges for profit/loss/flat, traded/no trades, and has errors/no errors. |
| P91 | Order fill details | Order rows show broker order id, filled quantity, filled price, and filled time when present. |
| P92 | Event payload preview | Event rows show a compact payload preview from `payload_json` without changing backend schema. |
| P93 | Snapshot delta context | Snapshot rows show trigger price, price-vs-trigger delta, consecutive losses, and snapshot time. |

## Non-goals

- No backend API changes.
- No database changes.
- No write actions.
- No changes to review export.
- No changes to trading, broker, runner, order, or risk logic.
- No charting library changes.

## Data Source

Existing `ReviewResponse` fields:

- `ReviewDay.llm_interactions`, `orders`, `events`, `snapshots`, `daily_pnl`, `trade_count`, `error_tags`.
- `ReviewOrder.broker_order_id`, `executed_quantity`, `executed_price`, `filled_at`.
- `ReviewEvent.payload_json`.
- `ReviewSnapshot.last_price`, `last_trigger_price`, `consecutive_losses`, `created_at`.

## UI Placement

- All UI changes live inside existing Review day cards in `frontend/src/views/Review.vue`.
- Existing runtime history and diagnostics cards stay unchanged.
- Existing day timeline sections remain visible; the new details augment rows rather than replacing them.

## Error Handling

- Missing order fill fields render as `-` or fall back to existing order values where already used.
- Invalid or empty `payload_json` renders as `payload -`.
- Snapshot delta only renders when `last_trigger_price > 0`; otherwise `Δ触发 -`.

## Testing

- Add Cypress coverage in `frontend/cypress/e2e/review_runtime_history.cy.ts` before production code.
- Use a deterministic review fixture with one day containing LLM, order, event, snapshot, daily PnL, and error tags.
- Verify RED first: new selectors/text fail because UI is missing.
- Verify GREEN with focused Review Cypress, `npm run type-check`, `npm run build`, and `git diff --check`.
