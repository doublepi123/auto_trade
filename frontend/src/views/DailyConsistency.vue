<script setup lang="ts">
import { computed, ref } from 'vue'
import { getDailyConsistencySummary, type DailyConsistencyResult } from '../api/dailyConsistency'

const loading = ref(false)
const result = ref<DailyConsistencyResult | null>(null)
const days = ref(90)

async function run() {
  loading.value = true
  try {
    result.value = await getDailyConsistencySummary({ days: days.value })
  } finally {
    loading.value = false
  }
}

const maxAbs = computed(() => {
  if (!result.value?.daily?.length) return 0
  return Math.max(...result.value.daily.map((d) => Math.abs(d.pnl)))
})

function pct(v: number | null): string {
  return v != null ? (v * 100).toFixed(1) + '%' : '—'
}

function pnlColor(v: number): string {
  return v > 0 ? '#67c23a' : v < 0 ? '#f56c6c' : '#909399'
}

function streakText(v: number): string {
  if (v > 0) return `连盈 ${v} 天`
  if (v < 0) return `连亏 ${-v} 天`
  return '—'
}
</script>

<template>
  <div class="page-container">
    <h2>每日盈亏一致性</h2>
    <p class="page-desc">按日聚合已实现净盈亏：盈利日占比、日 Sharpe、连盈/连亏与利润日集中度（灵感来自 QuantStats）</p>

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
                <span class="stat-label">盈利日占比</span>
                <span class="stat-value text-green">{{ pct(result.green_day_pct) }}</span>
                <span class="stat-sub">{{ result.green_days }} 盈 / {{ result.red_days }} 亏 / {{ result.trading_days }} 天</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">日 Sharpe（年化）</span>
                <span class="stat-value" :style="{ color: result.daily_sharpe != null ? pnlColor(result.daily_sharpe) : '#909399' }">
                  {{ result.daily_sharpe != null ? result.daily_sharpe.toFixed(2) : '—' }}
                </span>
                <span class="stat-sub">日均 {{ result.avg_daily_pnl.toFixed(2) }} · σ {{ result.daily_std.toFixed(2) }}</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">当前连势</span>
                <span class="stat-value">{{ streakText(result.current_streak) }}</span>
                <span class="stat-sub">最长连盈 {{ result.longest_green_streak }} · 连亏 {{ result.longest_red_streak }}</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">Top 5 日利润占比</span>
                <span class="stat-value">{{ pct(result.top5_day_profit_share) }}</span>
                <span class="stat-sub">最好 {{ result.best_day.date }} ({{ result.best_day.pnl }}) · 最差 {{ result.worst_day.date }} ({{ result.worst_day.pnl }})</span>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px" v-if="result.daily.length">
          <template #header>每日净盈亏</template>
          <div class="bar-chart">
            <div v-for="d in result.daily" :key="d.date" class="bar-item" :title="`${d.date}: ${d.pnl}`">
              <div class="bar-wrap">
                <div
                  class="bar"
                  :class="d.pnl >= 0 ? 'pos' : 'neg'"
                  :style="{ height: maxAbs > 0 ? (Math.abs(d.pnl) / maxAbs) * 50 + '%' : '0%' }"
                />
              </div>
              <span class="bar-label">{{ d.date.slice(5) }}</span>
            </div>
          </div>
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
.bar-chart { display: flex; gap: 2px; height: 180px; overflow-x: auto; }
.bar-item { display: flex; flex-direction: column; align-items: center; min-width: 16px; height: 100%; }
.bar-wrap { flex: 1; width: 100%; position: relative; }
.bar { position: absolute; left: 50%; transform: translateX(-50%); width: 10px; border-radius: 2px; }
.bar.pos { background: #67c23a; top: 0; }
.bar.neg { background: #f56c6c; bottom: 0; }
.bar-label { font-size: 9px; color: #909399; transform: rotate(-45deg); white-space: nowrap; margin-top: 4px; }
</style>
