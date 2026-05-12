import { createRouter, createWebHashHistory } from 'vue-router'
import Dashboard from '../views/Dashboard.vue'
import Strategy from '../views/Strategy.vue'
import TradeHistory from '../views/TradeHistory.vue'

const routes = [
  { path: '/', component: Dashboard },
  { path: '/strategy', component: Strategy },
  { path: '/history', component: TradeHistory },
]

export default createRouter({
  history: createWebHashHistory(),
  routes,
})
