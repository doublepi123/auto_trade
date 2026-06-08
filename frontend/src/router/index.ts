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
  { path: '/lab', component: () => import('../views/Lab.vue') },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

export default createRouter({
  history: createWebHashHistory(),
  routes,
})
