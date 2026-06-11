<template>
  <el-container class="app-container">
    <!-- 桌面端顶部导航 -->
    <el-header v-if="!isMobile" class="app-header" data-testid="desktop-nav">
      <h2>Auto Trade</h2>
      <nav class="app-menu" aria-label="主导航">
        <router-link to="/" class="app-menu-link" :class="{ active: route.path === '/' }">仪表盘</router-link>
        <router-link to="/watchlist" class="app-menu-link" :class="{ active: route.path === '/watchlist' }">观察列表</router-link>
        <router-link to="/review" class="app-menu-link" :class="{ active: route.path === '/review' }">复盘</router-link>
        <router-link to="/reports" class="app-menu-link" :class="{ active: route.path === '/reports' }">交易报告</router-link>
        <router-link to="/backtest" class="app-menu-link" :class="{ active: route.path === '/backtest' }">回测</router-link>
        <router-link to="/experiments" class="app-menu-link" :class="{ active: route.path === '/experiments' }">策略实验</router-link>
        <router-link to="/strategy" class="app-menu-link" :class="{ active: route.path === '/strategy' }">策略配置</router-link>
        <router-link to="/credentials" class="app-menu-link" :class="{ active: route.path === '/credentials' }">凭证设置</router-link>
        <router-link to="/history" class="app-menu-link" :class="{ active: route.path === '/history' }">交易历史</router-link>
        <router-link to="/events" class="app-menu-link" :class="{ active: route.path === '/events' }">决策时间线</router-link>
        <router-link to="/lab" class="app-menu-link" :class="{ active: route.path === '/lab' }">优化工作台</router-link>
      </nav>
      <el-button size="small" text @click="dialogVisible = true" data-testid="nav-notification-settings"
        >通知偏好</el-button
      >
    </el-header>
    <el-dialog v-model="dialogVisible" title="通知偏好" width="400px">
      <NotificationSettings />
    </el-dialog>

    <!-- 主内容区 -->
    <el-main :class="{ 'mobile-main': isMobile }">
      <router-view />
    </el-main>

    <!-- 移动端底部导航 -->
    <nav v-if="isMobile" class="bottom-nav" data-testid="bottom-nav">
      <router-link to="/" class="nav-item" :class="{ active: route.path === '/' }">
        <el-icon><Odometer /></el-icon>
        <span>驾驶舱</span>
      </router-link>
      <router-link to="/review" class="nav-item" :class="{ active: route.path === '/review' }">
        <el-icon><TrendCharts /></el-icon>
        <span>复盘</span>
      </router-link>
      <router-link to="/strategy" class="nav-item" :class="{ active: route.path === '/strategy' }">
        <el-icon><Setting /></el-icon>
        <span>策略</span>
      </router-link>
      <router-link to="/history" class="nav-item" :class="{ active: route.path === '/history' }">
        <el-icon><List /></el-icon>
        <span>历史</span>
      </router-link>
      <router-link to="/events" class="nav-item" :class="{ active: route.path === '/events' }">
        <el-icon><Clock /></el-icon>
        <span>时间线</span>
      </router-link>
      <router-link to="/credentials" class="nav-item" :class="{ active: route.path === '/credentials' }">
        <el-icon><Key /></el-icon>
        <span>设置</span>
      </router-link>
    </nav>
  </el-container>
</template>

<script setup lang="ts">
import { defineAsyncComponent, ref, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { Odometer, Setting, List, Clock, Key, TrendCharts } from '@element-plus/icons-vue'
import { useNotificationStream } from './composables/useNotificationStream'

const NotificationSettings = defineAsyncComponent(() => import('./components/NotificationSettings.vue'))

const route = useRoute()
const dialogVisible = ref(false)
const notifications = useNotificationStream()
const MOBILE_BREAKPOINT = 768
const isMobile = ref(window.innerWidth <= MOBILE_BREAKPOINT)

function handleResize() {
  isMobile.value = window.innerWidth <= MOBILE_BREAKPOINT
}

onMounted(() => {
  window.addEventListener('resize', handleResize)
  notifications.enable()
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
  notifications.disable()
})
</script>

<style scoped>
.app-container {
  min-height: 100vh;
}

.app-header {
  display: flex;
  align-items: center;
  gap: 16px;
}

.app-header h2 {
  flex: 0 0 auto;
  margin: 0;
  white-space: nowrap;
}

.app-menu {
  display: flex;
  align-items: center;
  gap: 20px;
  flex: 1 1 auto;
  min-width: 0;
}

.app-menu-link {
  color: #6b7280;
  font-size: 13px;
  text-decoration: none;
  white-space: nowrap;
}

.app-menu-link.active {
  color: #409eff;
}

/* 移动端主内容区增加底部 padding 给导航栏留空间 */
.mobile-main {
  padding-bottom: 72px;
}

/* 底部导航栏 */
.bottom-nav {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  z-index: 100;
  display: flex;
  align-items: center;
  justify-content: space-around;
  height: 60px;
  border-top: 1px solid #e1e7f0;
  background: #fff;
  box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.06);
}

.nav-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  width: 20%;
  height: 100%;
  color: #6b7280;
  font-size: 11px;
  text-decoration: none;
  transition: color 0.2s;
}

.nav-item .el-icon {
  font-size: 20px;
}

.nav-item.active {
  color: #409eff;
}

.nav-item:active {
  opacity: 0.7;
}
</style>
