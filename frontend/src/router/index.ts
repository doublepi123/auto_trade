import { createRouter, createWebHashHistory } from 'vue-router'
import Dashboard from '../views/Dashboard.vue'
import Credentials from '../views/Credentials.vue'
import Strategy from '../views/Strategy.vue'
import TradeHistory from '../views/TradeHistory.vue'

const routes = [
  { path: '/', component: Dashboard },
  { path: '/strategy', component: Strategy },
  { path: '/credentials', component: Credentials },
  { path: '/history', component: TradeHistory },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

export default createRouter({
  history: createWebHashHistory(),
  routes,
})
