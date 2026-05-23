<template>
  <el-container>
    <el-header class="app-header">
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
    <el-main>
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup lang="ts">
import { useRoute } from 'vue-router'

const route = useRoute()
</script>

<style scoped>
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
</style>
