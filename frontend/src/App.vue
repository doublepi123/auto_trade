<template>
  <el-container>
    <el-header>
      <h2>Auto Trade</h2>
      <el-menu mode="horizontal" :default-active="route.path" router>
        <el-menu-item index="/">Dashboard</el-menu-item>
        <el-menu-item index="/strategy">Strategy</el-menu-item>
        <el-menu-item index="/history">Trade History</el-menu-item>
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

<style>
body { margin: 0; font-family: sans-serif; }
.el-header { display: flex; align-items: center; gap: 24px; }
.el-header h2 { margin: 0; }
</style>
