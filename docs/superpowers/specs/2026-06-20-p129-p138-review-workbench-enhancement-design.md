# P129–P138 Review Workbench Enhancement Design

> Date: 2026-06-20  
> Scope: 10 low-risk frontend-only feature iterations for the Review page.  
> Decision: use current loaded review data only; do not add backend endpoints, database tables, broker calls, order writes, runner changes, or risk-controller changes.

## Context

The project already has a rich Review page (`frontend/src/views/Review.vue`) that loads `ReviewResponse` from `/api/review`, displays LLM interactions, orders, events, snapshots, runtime history, diagnostics, risk history, and JSON/CSV export. Recent batches focused on Notification Center, Watchlist, Reports, Dashboard, and cross-page UX. The next 10-feature batch should improve the Review page as a practical post-trade workbench without expanding backend risk.

This design intentionally follows the successful recent pattern:

- derive all new information from already-loaded `reviewData.days` and local UI state;
- keep labels explicit when results are based on “current query/current loaded range”;
- use Cypress first for visible behavior;
- keep implementation mostly inside `Review.vue`, adding shared helpers only if duplication appears.

## Goals

1. Make Review faster for daily trade/LLM/error triage.
2. Add 10 small, independently testable features that are useful even with partial data.
3. Keep all behavior read-only and reversible.
4. Preserve current exports and existing timeline layout.

## Non-Goals

- No backend aggregation endpoint.
- No new database model or migration.
- No full-text server search.
- No editing notes, orders, strategies, credentials, or alerts from Review.
- No charting dependency; any visual summary uses existing cards/tags/SVG patterns only if needed.
- No cross-page global state store.

## Feature Set

| ID | Feature | Source Data | User Value |
|---|---|---|---|
| P129 | Review health score card | days, pnl, errors, trades, LLM success/applied | One-glance quality indicator for the selected review range |
| P130 | Day quick filters | day pnl/trade/error/LLM/event counts | Quickly isolate losing days, error days, no-trade days, LLM-active days |
| P131 | Timeline keyword filter | LLM/order/event fields and payload text | Find relevant items in the current review result without another API call |
| P132 | LLM action summary | LLM `order_action`, `success`, `applied`, `order_status` | See how often the advisor suggested BUY/SELL/NONE and whether suggestions were applied |
| P133 | Order execution quality summary | order quantity, executed quantity, price, executed price, status | Surface filled/partial/open orders and approximate slippage from loaded orders |
| P134 | Event severity buckets | event type/message/payload/error tags | Group operational issues into risk/order/session/broker/other buckets |
| P135 | Snapshot volatility strip | snapshots last_price, trigger price, pnl, consecutive losses | Highlight price range, trigger distance, max consecutive losses in the review range |
| P136 | Copy review brief | derived summary text | Copy a concise human-readable review summary for notes/chat |
| P137 | Local CSV export for visible timeline | filtered visible days/items | Export the current filtered Review view without calling backend export |
| P138 | Review preference persistence | localStorage | Persist expanded compact mode, active day filter, and keyword between visits |

## UI / UX Design

### Layout

Add one compact “复盘洞察” area immediately after the existing top summary row and before runtime history cards. It should contain:

- health score card and range brief;
- LLM action summary chips;
- execution quality summary chips;
- snapshot volatility strip.

Add a lightweight toolbar above the day timeline:

- day filter buttons: 全部 / 亏损日 / 盈利日 / 有错误 / 有交易 / 无交易 / 有 LLM / 有事件;
- keyword input for current loaded timeline;
- compact mode toggle;
- copy brief and local CSV export buttons.

The existing day cards remain the main body. Filters only affect displayed day cards and derived “visible” export/summary; the original `reviewData` remains unchanged.

### Labeling

All derived/filter UI must explicitly say “当前查询结果” or “当前筛选结果” to avoid implying all-time analytics.

### Empty States

If filters hide all days, show a filtered empty state with a reset button instead of the generic no-data message.

## Data Model and Computed State

No API contract changes. Add local derived types inside `Review.vue` or a small local helper section:

- `reviewHealth`: score 0–100, label, reasons.
- `filteredDays`: days after day quick filter and keyword filter.
- `llmActionSummary`: counts by action plus success/applied counts.
- `executionQuality`: filled, partial, pending/open, failed/rejected counts, approximate slippage samples where both `price` and `executed_price` exist.
- `eventBuckets`: risk/order/session/broker/llm/other using deterministic keyword/type matching.
- `snapshotSummary`: min/max last price, largest trigger distance, max consecutive losses, latest snapshot.
- `reviewBriefText`: plain text generated from current query, health score, PnL, trades, errors, LLM summary.
- `visibleTimelineRows`: flattened rows for local CSV export.

All computations must tolerate missing arrays, null execution fields, empty snapshots, malformed payload JSON, and unknown event types.

## Feature Details

### P129 Review Health Score

Score formula should be simple and explainable, not a trading recommendation:

- start at 100;
- subtract for negative total PnL, error-tag presence, failed/rejected orders, consecutive loss pressure, and failed LLM interactions;
- clamp 0–100;
- labels: 健康 / 需关注 / 高风险.

Show top 2–3 reasons as tags. This is diagnostic only.

### P130 Day Quick Filters

Filter by day-level properties already present in `ReviewDay`: PnL, trade_count, error_tags, llm_interactions, events. Reset button clears day filter and keyword.

### P131 Timeline Keyword Filter

Search current loaded Review data across:

- day date and symbol;
- LLM symbol/action/status;
- order id/symbol/side/status;
- event type/message/payload_json;
- snapshot engine state.

This is client-side only and case-insensitive.

### P132 LLM Action Summary

Aggregate visible days’ LLM interactions by `order_action`. Show counts for BUY/SELL/SHORT/COVER/NONE/other, success rate, applied count, and order-linked count.

### P133 Order Execution Quality

Aggregate visible orders by status and execution completeness:

- filled if status includes `FILLED` or executed_quantity is positive and complete;
- partial if status includes `PARTIAL` or executed quantity is between 0 and quantity;
- open if status suggests submitted/new/pending;
- failed if status suggests failed/rejected/cancelled.

Approximate slippage is `executed_price - price` when both numbers are finite. Direction-normalized slippage is explicitly out of scope for this batch because Review order rows do not include full trade intent/side semantics beyond simple side labels.

### P134 Event Severity Buckets

Bucket current visible events by deterministic matching:

- Risk: event type/message contains `RISK`, `LOSS`, `KILL`, `PAUSE`.
- Order: contains `ORDER`, `FILLED`, `REJECT`, `CANCEL`, `TIMEOUT`.
- Session: contains `SESSION`, `RTH`, `MARKET`.
- Broker: contains `BROKER`, `QUOTE`, `LONGPORT`, `STREAM`.
- LLM: contains `LLM`, `ADVISOR`, `INTERVAL`.
- Other: fallback.

This is a triage helper, not an authoritative backend severity taxonomy.

### P135 Snapshot Volatility Strip

For visible snapshots:

- show min/max last price;
- show latest engine state;
- show max consecutive losses;
- show max absolute distance between last_price and last_trigger_price when trigger price > 0.

If no snapshots exist, show “无快照样本”.

### P136 Copy Review Brief

Use Clipboard API with fallback error message. The brief includes range, symbol, health label/score, total PnL, trades, visible day count, error tags, LLM action counts, and execution quality counts.

### P137 Local CSV Export for Visible Timeline

Reuse `frontend/src/utils/csv.ts` if available. Export flattened rows with columns:

- date, source, symbol, type, status, side, message, pnl, created_at, broker_order_id.

Rows include LLM interactions, orders, events, and snapshots from visible days. This is separate from backend `导出 CSV`, which exports the full backend review result.

### P138 Review Preference Persistence

Persist only harmless local UI preferences:

- compact mode;
- selected day filter;
- keyword.

Use a versioned localStorage key such as `auto_trade.review.workbench.v1`. Guard JSON parse/write failures and ignore invalid values.

## Architecture

Primary implementation target:

- `frontend/src/views/Review.vue`

Likely test target:

- `frontend/cypress/e2e/review_workbench.cy.ts` or extend an existing Review spec if one is clearly closest.

Potential shared helper:

- `frontend/src/utils/csv.ts` reuse only; do not add a new shared module unless repeated code becomes substantial.

The component remains a single page with local refs/computed values. No Pinia/Vuex. No backend API module changes.

## Data Flow

1. User runs the existing Review search.
2. `reviewData` loads from the existing API.
3. New computed values derive health, filters, summaries, buckets, and export rows from `reviewData.days`.
4. User changes local filters/keyword/compact mode.
5. `filteredDays`, summaries, brief, and local export update reactively.
6. Backend export buttons keep using existing API behavior; local export uses only filtered client rows.

## Error Handling

- Empty review result: keep current empty behavior.
- Filtered result empty: show resettable filtered-empty message.
- Clipboard failure: show `ElMessage.error` with actionable text.
- CSV export with no visible rows: disable button or show warning.
- localStorage parse/write failure: ignore and keep defaults; do not block page.
- Malformed event payload JSON: treat as raw string for search/export.
- Non-finite numeric fields: skip from numeric aggregates and show `-` where needed.

## Testing Strategy

Use TDD with Cypress first.

Add a focused Cypress spec that proves all 10 user-visible behaviors:

1. Health score card renders and changes label/reasons from stubbed data.
2. Day quick filter isolates losing/error/no-trade/LLM-active days.
3. Keyword filter searches order/event/LLM text and shows filtered empty state.
4. LLM action summary displays action counts and applied/success metrics.
5. Execution quality summary displays filled/partial/open/failed counts.
6. Event buckets group risk/order/session/broker/other examples.
7. Snapshot strip displays range, latest state, max losses, trigger distance.
8. Copy brief calls Clipboard API and shows success/failure feedback.
9. Local CSV export emits visible filtered rows.
10. Preferences persist across reload.

Also run:

- `cd frontend && npm run type-check`
- `cd frontend && npm run build`
- Backend tests are not required for frontend-only changes, but if any backend/test/config files are touched, run targeted pytest and then full backend pytest.

## Rollout / Risk

Risk is low because this is frontend-only and read-only. Main risks:

- Review page becomes visually dense.
- Derived labels may be mistaken for authoritative backend analytics.
- localStorage persistence may surprise users.

Mitigations:

- Keep new area compact and collapsible via compact mode.
- Label all derived values as “当前查询/筛选”.
- Persist only UI preferences, not data.

## Acceptance Criteria

- P129–P138 are visible and covered by Cypress.
- Existing Review search/export still works.
- No new API endpoints or schema changes.
- `npm run type-check` and `npm run build` pass.
- Cypress focused Review spec passes.
- The implementation stays scoped to frontend Review behavior plus tests.

## Explicit YAGNI

- No server-side review analytics.
- No all-time or cross-symbol aggregation.
- No new chart library.
- No editing or action buttons in Review.
- No strategy recommendations from the health score.
- No backend CSV export contract changes.
