<template>
  <el-container class="app-container">
    <!-- 桌面端顶部导航 -->
    <el-header v-if="!isMobile" class="app-header" data-testid="desktop-nav">
      <h2>Auto Trade</h2>
      <el-menu class="app-menu" mode="horizontal" :default-active="route.path" router>
        <el-menu-item index="/">仪表盘</el-menu-item>
        <el-menu-item index="/backtest">回测</el-menu-item>
        <el-menu-item index="/strategy">策略配置</el-menu-item>
        <el-menu-item index="/credentials">凭证设置</el-menu-item>
        <el-menu-item index="/history">交易历史</el-menu-item>
        <el-menu-item index="/events">决策时间线</el-menu-item>
      </el-menu>
    </el-header>

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
import { ref, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { Odometer, Setting, List, Clock, Key } from '@element-plus/icons-vue'

const route = useRoute()
const MOBILE_BREAKPOINT = 768
const isMobile = ref(window.innerWidth <= MOBILE_BREAKPOINT)

function handleResize() {
  isMobile.value = window.innerWidth <= MOBILE_BREAKPOINT
}

onMounted(() => {
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
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
  flex: 1 1 auto;
  min-width: 0;
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
