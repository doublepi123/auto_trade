<script setup lang="ts">
import { ref } from 'vue'
import { getExitEfficiencySummary, type ExitEfficiencyResult } from '../api/exitEfficiency'

const loading = ref(false)
const result = ref<ExitEfficiencyResult | null>(null)
const days = ref(90)

async function run() {
  loading.value = true
  try {
    result.value = await getExitEfficiencySummary({ days: days.value })
  } finally {
    loading.value = false
  }
}

function pct(v: number | null): string {
  return v != null ? (v * 100).toFixed(1) + '%' : '—'
}

function num(v: number | null): string {
  return v != null ? v.toFixed(2) : '—'
}

function pnlColor(v: number): string {
  return v > 0 ? '#67c23a' : v < 0 ? '#f56c6c' : '#909399'
}
</script>

<template>
  <div class="page-container">
    <h2>离场效率分析</h2>
    <p class="page-desc">止盈捕获率（净盈 / 最大有利 excursion）、回吐与不利 excursion 容忍（灵感来自 Edgewonk / TraderVue）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="回看天数">
          <el-input-number v-model="days" :min="7" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">分析</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="result">
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">平均捕获率</span>
                <span class="stat-value text-green">{{ pct(result.avg_capture_rate) }}</span>
                <span class="stat-sub">中位 {{ pct(result.median_capture_rate) }}</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">平均回吐</span>
                <span class="stat-value text-red">{{ num(result.avg_giveback) }}</span>
                <span class="stat-sub">中位 {{ num(result.median_giveback) }}</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">桌上留钱比例</span>
                <span class="stat-value">{{ pct(result.left_on_table_pct) }}</span>
                <span class="stat-sub">MFE &gt; 2×净盈的盈利单 {{ result.left_on_table_count }} 笔</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">盈利单平均 MAE 深度</span>
                <span class="stat-value">{{ num(result.avg_winner_mae_depth) }}</span>
                <span class="stat-sub">全部 {{ num(result.avg_mae_depth) }}</span>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>按离场原因</template>
          <el-table :data="result.by_exit_cause" size="small" max-height="480">
            <el-table-column prop="exit_cause" label="离场原因" width="160" />
            <el-table-column prop="trades" label="笔数" width="80" />
            <el-table-column prop="net_pnl" label="净 PnL" width="120">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.net_pnl) }">{{ row.net_pnl }}</span>
              </template>
            </el-table-column>
            <el-table-column label="平均捕获率" width="120">
              <template #default="{ row }">{{ pct(row.avg_capture) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </template>
    </template>
  </div>
</template>

<style scoped>
.page-container { padding: 20px; }
.page-desc { color: #909399; margin-bottom: 16px; }
.control-card { margin-bottom: 8px; }
.stat-custom { display: flex; flex-direction: column; align-items: center; gap: 4px; }
.stat-label { font-size: 12px; color: #909399; }
.stat-value { font-size: 20px; font-weight: 600; }
.stat-sub { font-size: 12px; color: #606266; }
.text-green { color: #67c23a; }
.text-red { color: #f56c6c; }
</style>
