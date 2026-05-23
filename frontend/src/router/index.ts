import { createRouter, createWebHashHistory } from 'vue-router'
import Dashboard from '../views/Dashboard.vue'
import Backtest from '../views/Backtest.vue'
import Credentials from '../views/Credentials.vue'
import DecisionTimeline from '../views/DecisionTimeline.vue'
import Strategy from '../views/Strategy.vue'
import TradeHistory from '../views/TradeHistory.vue'

const routes = [
  { path: '/', component: Dashboard },
  { path: '/backtest', component: Backtest },
  { path: '/strategy', component: Strategy },
  { path: '/credentials', component: Credentials },
  { path: '/history', component: TradeHistory },
  { path: '/events', component: DecisionTimeline },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

export default createRouter({
  history: createWebHashHistory(),
  routes,
})
