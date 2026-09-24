# frontend/src/components/

## Responsibility

18 shared SFCs (~3.8k lines). Two roles: **chart primitives** (hand-written SVG — no chart library is allowed) and **reusable panels** for data-heavy surfaces that appear on more than one page (or that keep `Dashboard.vue`/`Watchlist.vue`/`Lab.vue` from growing further). Cross-page contracts that Cypress depends on live here (`DataState.vue` testids).

## Design

- **No chart library.** `PriceChart`, `PnLChart`, `EquityCurvePanel`, `RiskHistoryPanel`, `BacktestChart` render pure SVG path/polygon markup with computed scales. Most analytics views don't chart at all — they are `el-table` + metric cards.
- **Shared display contracts:**
  - `DataState.vue` renders error → loading → empty → default slot in that strict order, exposing `data-testid="data-state-error|loading|empty"`. Cypress specs assert these; never remove the testids.
  - `MetricStat.vue` — the metric-card unit (label + value + optional tone).
  - `StatisticsQualityAlert.vue` — surfaces `statistics_quality` evidence returned by research endpoints; consumed by 33 views, making it the widest-reach component.
- **Copy discipline:** user-facing enum labels come from `utils/labels.ts` (`engineStateLabel`, `skipCategoryLabel`, …); components must not hardcode Chinese enum copy.
- Components are leaf-ish: they receive props/emit events and own no API calls except the health panels, which fetch their own slice (cron/quote/reconciliation/version status) so Dashboard composition stays declarative.

## Flow

A view imports a panel, passes typed props (rows, series, config), and the panel renders independently. Health panels (`CronHealthPanel`, `QuoteHealthPanel`, `ReconciliationStatus`, `SessionClockPanel`, `SymbolAttributionPanel`) self-poll their endpoint on mount and render an `el-tag`/card verdict — the Dashboard is a grid of these plus the charts. `CommandPalette.vue` is mounted once in `App.vue` and bridges to active views through `composables/useCommandPalette` + `useViewRefreshRegistry` rather than props.

## Integration

Consumers (from grep of `views/*.vue` + `App.vue`):

| Component | Role | Used by |
|---|---|---|
| `StatisticsQualityAlert.vue` | banner for `statistics_quality` evidence on research pages | 33 analytics views + `Reports`, `Review`, `Dashboard`, `TradeHistory` |
| `DataState.vue` | error/loading/empty/default-slot gate with Cypress testids | `Lab`, `NotificationCenter`, `PrimaryCandidacy`, `Watchlist` |
| `MetricStat.vue` | metric card | `Lab`, `App` (palette header) |
| `PriceChart.vue` | SVG price + band chart | `Dashboard`, `Review` |
| `PnLChart.vue` | SVG PnL bar/line chart | `Dashboard`, `Review` |
| `EquityCurvePanel.vue` | equity curve panel | `Dashboard` |
| `RiskHistoryPanel.vue` | risk-state history chart | `Dashboard`, `Review` |
| `BacktestChart.vue` | backtest equity/price overlay | `Backtest` |
| `PositionPnlPanel.vue` | open-position PnL card | `Dashboard` |
| `SessionClockPanel.vue` | market session clock | `Dashboard` |
| `CronHealthPanel.vue` | cron job health | `Dashboard` |
| `QuoteHealthPanel.vue` | quote feed health | `Dashboard` |
| `ReconciliationStatus.vue` | order reconciliation gate status (+force-resume) | `Dashboard` |
| `SymbolAttributionPanel.vue` | per-symbol attribution summary | `Dashboard` |
| `InterventionEvidencePanel.vue` | intervention evidence detail | `DecisionTimeline` |
| `NotificationSettings.vue` | notification preferences form | `App` |
| `CommandPalette.vue` | Cmd/Ctrl+K palette (nav + actions) | `App` |
| `CopyButton.vue` | copy-to-clipboard button | `DecisionTimeline`, `TradeHistory` |

When a new analytics view is needed, reach for `DataState` + `MetricStat` + `StatisticsQualityAlert` + `el-table` before writing new markup; extract a component here (not inline markup) when `Watchlist.vue` (4799 lines) or `Lab.vue` (4677) would otherwise grow.
