<template>
  <div class="lab-page" data-testid="lab-page">
    <h2>LLM 优化工作台</h2>
    <el-tabs v-model="activeTab" data-testid="lab-tabs">
      <el-tab-pane label="实验与版本" name="experiments">
        <div data-testid="tab-experiments">实验与版本（待填充）</div>
      </el-tab-pane>
      <el-tab-pane label="性能看板" name="performance">
        <div data-testid="tab-performance">性能看板（待填充）</div>
      </el-tab-pane>
      <el-tab-pane label="指标面板" name="indicators">
        <div data-testid="tab-indicators">指标面板（待填充）</div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'

const activeTab = ref('experiments')
</script>

<style scoped>
.lab-page {
  padding: 16px;
}
</style>
