<script setup lang="ts">
import { ref } from 'vue'
import { getReturnCalendar, type ReturnCalendarResult } from '../api/returnCalendar'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'

const loading = ref(false)
const result = ref<ReturnCalendarResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(365)
const view = ref<'weekly' | 'monthly'>('monthly')

async function run() {
  loading.value = true
  try {
    result.value = await getReturnCalendar({
      symbol: symbol.value || undefined,
      lookback_days: lookbackDays.value,
    })
  } finally {
    loading.value = false
  }
}

function pnlColor(v: number): string {
  return v > 0 ? '#67c23a' : v < 0 ? '#f56c6c' : '#909399'
}

function barWidth(v: number, data: { pnl: number }[]): number {
  const maxAbs = Math.max(...data.map((d) => Math.abs(d.pnl)), 1)
  return Math.round((Math.abs(v) / maxAbs) * 100)
}
</script>

<template>
  <div class="page-container">
    <h2>收益日历</h2>
    <p class="page-desc">按 ISO 周和自然月聚合已实现 PnL（灵感来自 QuantStats 月度收益表）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="symbol" placeholder="全部" clearable style="width: 140px" />
        </el-form-item>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="30" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">计算</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="result">
      <StatisticsQualityAlert :quality="result.statistics_quality" />
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="周胜率" :value="result.summary.weekly_win_rate * 100" :precision="1" suffix="%" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="月胜率" :value="result.summary.monthly_win_rate * 100" :precision="1" suffix="%" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">最佳月</span>
                <span class="stat-value" v-if="result.summary.best_month">{{ result.summary.best_month.period }} ({{ result.summary.best_month.pnl }})</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">最差月</span>
                <span class="stat-value" v-if="result.summary.worst_month">{{ result.summary.worst_month.period }} ({{ result.summary.worst_month.pnl }})</span>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>
            <div class="card-header">
              <span>PnL 时间线</span>
              <el-radio-group v-model="view" size="small">
                <el-radio-button value="monthly">月</el-radio-button>
                <el-radio-button value="weekly">周</el-radio-button>
              </el-radio-group>
            </div>
          </template>
          <el-table :data="view === 'monthly' ? result.monthly.slice().reverse() : result.weekly.slice().reverse()" size="small" max-height="500">
            <el-table-column prop="period" label="周期" width="120" />
            <el-table-column prop="trade_count" label="交易数" width="80" />
            <el-table-column prop="pnl" label="PnL" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.pnl), fontWeight: 600 }">{{ row.pnl }}</span>
              </template>
            </el-table-column>
            <el-table-column label="柱状">
              <template #default="{ row }">
                <el-progress
                  :percentage="barWidth(row.pnl, view === 'monthly' ? result.monthly : result.weekly)"
                  :show-text="false"
                  :stroke-width="14"
                  :color="pnlColor(row.pnl)"
                />
              </template>
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
.stat-custom { display: flex; flex-direction: column; }
.stat-label { font-size: 12px; color: #909399; }
.stat-value { font-size: 14px; font-weight: 600; margin-top: 4px; }
.card-header { display: flex; justify-content: space-between; align-items: center; }
</style>
