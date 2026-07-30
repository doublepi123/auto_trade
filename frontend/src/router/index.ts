import { createRouter, createWebHashHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  { path: '/', component: () => import('../views/Dashboard.vue') },
  { path: '/backtest', component: () => import('../views/Backtest.vue') },
  { path: '/experiments', component: () => import('../views/Experiments.vue') },
  { path: '/strategy', component: () => import('../views/Strategy.vue') },
  { path: '/credentials', component: () => import('../views/Credentials.vue') },
  { path: '/history', component: () => import('../views/TradeHistory.vue') },
  { path: '/events', component: () => import('../views/DecisionTimeline.vue') },
  { path: '/review', component: () => import('../views/Review.vue') },
  { path: '/reports', component: () => import('../views/Reports.vue') },
  { path: '/watchlist', component: () => import('../views/Watchlist.vue') },
  { path: '/alerts', component: () => import('../views/AlertRules.vue') },
  { path: '/notifications', component: () => import('../views/NotificationCenter.vue') },
  { path: '/lab', component: () => import('../views/Lab.vue') },
  { path: '/signal-consensus', component: () => import('../views/SignalConsensus.vue') },
  { path: '/universe-explainer', component: () => import('../views/UniverseExplainer.vue') },
  { path: '/risk-timeline', component: () => import('../views/RiskTimeline.vue') },
  { path: '/platform-catalog', component: () => import('../views/PlatformCatalog.vue') },
  { path: '/attribution', component: () => import('../views/Attribution.vue') },
  { path: '/regime', component: () => import('../views/RegimePanel.vue') },
  { path: '/drawdown', component: () => import('../views/DrawdownAnalysis.vue') },
  { path: '/strategy-health', component: () => import('../views/StrategyHealth.vue') },
  { path: '/execution-quality', component: () => import('../views/ExecutionQuality.vue') },
  { path: '/decision-replay', component: () => import('../views/DecisionReplay.vue') },
  { path: '/lookahead-analysis', component: () => import('../views/LookaheadAnalysis.vue') },
  { path: '/monte-carlo', component: () => import('../views/MonteCarlo.vue') },
  { path: '/correlation', component: () => import('../views/CorrelationMatrix.vue') },
  { path: '/kelly', component: () => import('../views/KellySizing.vue') },
  { path: '/streaks', component: () => import('../views/StreakAnalysis.vue') },
  { path: '/time-performance', component: () => import('../views/TimePerformance.vue') },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

export default createRouter({
  history: createWebHashHistory(),
  routes,
})
