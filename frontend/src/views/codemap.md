# frontend/src/views/

## Responsibility

64 page components (~31.9k lines) — the entire UI surface. Two distinct populations:

1. **13 core operating pages** — mutable, action-bearing surfaces for running the system: Dashboard, Watchlist, Review, Reports, Strategy, History, Events, Backtest, Experiments, Credentials, Alerts, Notifications, Lab.
2. **~51 read-only analytics pages** — each renders exactly one research view backed by one `/api/*` endpoint family (edge quality, drawdown duration, regime sensitivity, skip analytics, …). They take filter inputs and display results; they never mutate anything.

## Design

- **Resolve pages through the router, not filenames.** Path ≠ file for many routes: `/events`→`DecisionTimeline.vue`, `/history`→`TradeHistory.vue`, `/alerts`→`AlertRules.vue`, `/notifications`→`NotificationCenter.vue`, `/regime`→`RegimePanel.vue`, `/kelly`→`KellySizing.vue`, `/correlation`→`CorrelationMatrix.vue`, `/benchmark`→`BenchmarkAlphaBeta.vue`. Also, `api/strategy.ts` (live config) is unrelated to `views/Strategy.vue`.
- **Analytics-page pattern** (the template to copy — see `EdgeQuality.vue`, 33 cousins): `<script setup lang="ts">` + a local `ref` state block (`loading`, `result`, filter `ref`s) + one imported api-client function + a `run()` that awaits it in `try/finally` + `<el-card>` filter form + `StatisticsQualityAlert` + `el-table`/metric cards for output. No composable needed unless state must survive navigation; no store writes.
- **Charts are pure SVG** (`PriceChart`, `PnLChart`, `EquityCurvePanel`, `RiskHistoryPanel`, `BacktestChart` components) — no chart library. Most analytics pages are tables + cards only (only `Reports`, `AlertRules`, `NotificationCenter`, `ProfitConcentration` contain inline `<svg>` beyond the shared components).
- **Language:** UI copy is Chinese; enum labels route through `utils/labels.ts` (`skipCategoryLabel`, `engineStateLabel`, `orderStatusLabel`, …) rather than being hardcoded per view.
- **Size ceiling:** `Watchlist.vue` (4799) and `Lab.vue` (4677) are the two giants; the rule is extract a component/composable instead of growing them further. `Dashboard.vue` (2720) is a grid of the shared panels precisely to stay composable.
- **Refresh wiring:** pages that own a reload register it via `useRegisterViewRefresh` so the command palette's reload action targets the mounted view (`Dashboard`, `Reports`, `Watchlist`).

## Flow

`router/index.ts` lazy-imports a view (`() => import('../views/X.vue')`) → the view's setup composes: `useConnectionHealth`/`useDashboardData` (Dashboard), a domain api client, and shared panels → data lands in local refs → template renders `el-table`/SVG/metric cards. Analytics pages run their fetch on mount and on filter submit; no global cache (each visit refetches). Mutations on core pages call api-client POST/PUT endpoints and refresh locally; nothing here writes composables except the documented hand-offs (`useSymbolStore`, `useRegisterViewRefresh`).

## Integration

- **Upstream:** `src/router/index.ts` (flat table, hash history, catch-all → `/`); `App.vue` shell provides palette, theme, session clock, notification stream.
- **Data:** `src/api/*` per-domain clients only. Views never construct URLs.
- **Shared UI:** `src/components` panels/charts; `StatisticsQualityAlert` appears in 33 of these views because research endpoints return `statistics_quality` evidence by contract.
- **Tests:** `cypress/e2e/**` visits these routes with every `/api` call stubbed by `cy.stubApi()`; `DataState` testids are part of that contract (used by `Lab`, `NotificationCenter`, `PrimaryCandidacy`, `Watchlist`).

### Core operating pages

| View | Route | Purpose |
|---|---|---|
| `Dashboard.vue` | `/` | 交易驾驶舱: live status, price/PnL charts, equity curve, risk history, session clock, cron/quote/reconciliation health panels, pinned symbols |
| `Watchlist.vue` | `/watchlist` | watchlist quant scores, quotes, snapshots; largest view (4799 lines) |
| `Review.vue` | `/review` | 复盘工作台: trade replay with price/PnL/risk charts + diagnostics snapshot |
| `Reports.vue` | `/reports` | 交易报告: daily/weekly/monthly/range reports + schedule + CSV export |
| `Strategy.vue` | `/strategy` | 策略配置: live strategy config form (via `useFormState`) |
| `TradeHistory.vue` | `/history` | closed-trade table with persisted column visibility + CSV export |
| `DecisionTimeline.vue` | `/events` | 决策时间线: trade_events feed with intervention-evidence panel + copy buttons |
| `Backtest.vue` | `/backtest` | 回测: backtest runner + `BacktestChart` |
| `Experiments.vue` | `/experiments` | 策略实验: strategy-experiments registry + LLM evaluations |
| `Credentials.vue` | `/credentials` | broker credential management + notification channel test |
| `AlertRules.vue` | `/alerts` | 告警规则: alert-rule CRUD |
| `NotificationCenter.vue` | `/notifications` | notification log/stats/preferences + blob export |
| `Lab.vue` | `/lab` | LLM 优化工作台: indicator lab, performance compare, LLM usage (4677 lines) |

### Analytics pages (read-only research views)

| View | Route | Purpose |
|---|---|---|
| `SignalConsensus.vue` | `/signal-consensus` | 信号共识矩阵 |
| `UniverseExplainer.vue` | `/universe-explainer` | Universe 选择解释器 |
| `RiskTimeline.vue` | `/risk-timeline` | 风控检查时间线 |
| `PlatformCatalog.vue` | `/platform-catalog` | 平台分析模块目录 |
| `Attribution.vue` | `/attribution` | 绩效归因分析 |
| `RegimePanel.vue` | `/regime` | 市场状态面板 |
| `DrawdownAnalysis.vue` | `/drawdown` | 回撤分析 |
| `StrategyHealth.vue` | `/strategy-health` | 策略健康度监控 |
| `ExecutionQuality.vue` | `/execution-quality` | 执行质量分析 |
| `DecisionReplay.vue` | `/decision-replay` | 交易决策回放 |
| `LookaheadAnalysis.vue` | `/lookahead-analysis` | 前瞻偏差分析 |
| `MonteCarlo.vue` | `/monte-carlo` | 蒙特卡洛模拟 |
| `CorrelationMatrix.vue` | `/correlation` | 相关性矩阵 |
| `KellySizing.vue` | `/kelly` | Kelly 仓位定尺 |
| `StreakAnalysis.vue` | `/streaks` | 连胜连败分析 |
| `TimePerformance.vue` | `/time-performance` | 时段绩效 |
| `RollingMetrics.vue` | `/rolling-metrics` | 滚动绩效指标 |
| `RecoveryTimeline.vue` | `/recovery` | 回撤恢复时间线 |
| `BenchmarkAlphaBeta.vue` | `/benchmark` | 基准 Alpha/Beta |
| `TagAnalytics.vue` | `/tag-analytics` | 标签绩效 |
| `RiskScore.vue` | `/risk-score` | 综合风险评分 |
| `HoldingTime.vue` | `/holding-time` | 持仓时长分析 |
| `DistributionShape.vue` | `/distribution-shape` | PnL 分布形态 |
| `TradeFrequency.vue` | `/trade-frequency` | 交易频率分析 |
| `ProfitFactor.vue` | `/profit-factor` | 盈亏因子分解 |
| `Concentration.vue` | `/concentration` | 标的集中度 |
| `Autocorrelation.vue` | `/autocorrelation` | PnL 自相关 |
| `SizeImpact.vue` | `/size-impact` | 仓位规模影响 |
| `ReturnCalendar.vue` | `/return-calendar` | 收益日历 |
| `EdgeQuality.vue` | `/edge-quality` | 优势质量评分 |
| `DecayDetection.vue` | `/decay-detection` | 策略衰减检测 |
| `RollingVar.vue` | `/rolling-var` | 滚动 VaR/CVaR |
| `Asymmetry.vue` | `/asymmetry` | 胜负不对称性 |
| `CapitalEfficiency.vue` | `/capital-efficiency` | 闭环资金效率 |
| `IntradaySeasonality.vue` | `/intraday-seasonality` | 日内季节性 |
| `DrawdownDuration.vue` | `/drawdown-duration` | 回撤持续期 |
| `PredictionScore.vue` | `/prediction-score` | 条件胜率评分 |
| `RegimeSensitivity.vue` | `/regime-sensitivity` | 策略历史结果波动状态敏感性 |
| `Robustness.vue` | `/robustness` | 策略稳健性指数 |
| `Milestones.vue` | `/milestones` | PnL 里程碑 |
| `MomentumRanking.vue` | `/momentum-ranking` | 标的动量排名 |
| `FeeDrag.vue` | `/fee-drag` | 费用拖累分析 |
| `ExitEfficiency.vue` | `/exit-efficiency` | 离场效率分析 |
| `SkipAnalytics.vue` | `/skip-analytics` | 跳过原因分析 (skip categories FEE/REPRICING/COOLDOWN/RISK/…) |
| `RMultiples.vue` | `/r-multiples` | R 倍数分布 |
| `ProfitConcentration.vue` | `/profit-concentration` | 盈利集中度 |
| `ScratchAnalysis.vue` | `/scratch-analysis` | 保本交易分析 |
| `ReentryAnalysis.vue` | `/reentry-analysis` | 再入场行为 |
| `FirstTrade.vue` | `/first-trade` | 每日首笔平仓效应 |
| `LossContainment.vue` | `/loss-containment` | 亏损控制分析 |
| `DailyConsistency.vue` | `/daily-consistency` | 每日盈亏一致性 |
| `PrimaryCandidacy.vue` | `/primary-candidacy` | 主标的候选 (universe primary-candidacy) |
