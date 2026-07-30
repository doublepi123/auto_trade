<script setup lang="ts">
import { ref } from 'vue'
import { getRollingMetrics, type RollingMetricsResult } from '../api/rollingMetrics'

const loading = ref(false)
const result = ref<RollingMetricsResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(180)
const window = ref(20)

async function run() {
  loading.value = true
  try {
    result.value = await getRollingMetrics({
      symbol: symbol.value || undefined,
      lookback_days: lookbackDays.value,
      window: window.value,
    })
  } finally {
    loading.value = false
  }
}

const trendLabel: Record<string, string> = {
  improving: '改善中',
  decaying: '衰减中',
  stable: '稳定',
  insufficient: '样本不足',
}

function trendType(t: string): 'success' | 'danger' | 'info' | 'warning' {
  if (t === 'improving') return 'success'
  if (t === 'decaying') return 'danger'
  return 'info'
}
</script>

<template>
  <div class="page-container">
    <h2>滚动绩效指标</h2>
    <p class="page-desc">滑动窗口 Sharpe / 胜率 / 平均 PnL，识别策略状态变化与衰减（灵感来自 VectorBT）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="symbol" placeholder="全部" clearable style="width: 140px" />
        </el-form-item>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="7" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item label="窗口">
          <el-input-number v-model="window" :min="5" :max="200" :step="5" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">计算</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="result">
      <el-alert
        v-if="result.error"
        :title="result.error"
        type="warning"
        :closable="false"
        style="margin-top: 16px"
      />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="最新 Sharpe" :value="result.summary.latest_sharpe" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="Sharpe 均值" :value="result.summary.sharpe_mean" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="最新窗口胜率" :value="result.summary.latest_win_rate * 100" :precision="1" suffix="%" />
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="窗口胜率均值" :value="result.summary.win_rate_mean * 100" :precision="1" suffix="%" />
            </el-card>
          </el-col>
          <el-col :span="4">
            <el-card shadow="hover">
              <div class="trend-card">
                <span class="trend-label">趋势</span>
                <el-tag :type="trendType(result.summary.trend)" size="large">
                  {{ trendLabel[result.summary.trend] ?? result.summary.trend }}
                </el-tag>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>滚动 Sharpe 序列（最近 50 个窗口）</template>
          <div class="spark-row">
            <div
              v-for="(pt, i) in result.points.slice(-50)"
              :key="i"
              class="spark-bar"
              :class="pt.sharpe >= 0 ? 'bar-pos' : 'bar-neg'"
              :style="{ height: Math.min(Math.abs(pt.sharpe) * 20, 60) + 'px' }"
              :title="`#${pt.index} Sharpe=${pt.sharpe} WR=${(pt.win_rate * 100).toFixed(0)}%`"
            />
          </div>
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>滚动窗口明细（最近 30 条）</template>
          <el-table :data="result.points.slice(-30).reverse()" size="small" max-height="400">
            <el-table-column prop="index" label="#" width="60" />
            <el-table-column prop="total_pnl" label="窗口 PnL" width="100" />
            <el-table-column prop="avg_pnl" label="平均 PnL" width="100" />
            <el-table-column prop="win_rate" label="胜率" width="100">
              <template #default="{ row }">{{ (row.win_rate * 100).toFixed(1) }}%</template>
            </el-table-column>
            <el-table-column prop="sharpe" label="Sharpe" width="100">
              <template #default="{ row }">
                <span :style="{ color: row.sharpe >= 0 ? '#67c23a' : '#f56c6c' }">{{ row.sharpe }}</span>
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
.trend-card { display: flex; flex-direction: column; align-items: center; gap: 6px; }
.trend-label { font-size: 12px; color: #909399; }
.spark-row { display: flex; align-items: flex-end; gap: 2px; height: 70px; }
.spark-bar { width: 8px; border-radius: 2px 2px 0 0; min-height: 2px; }
.bar-pos { background: #67c23a; }
.bar-neg { background: #f56c6c; }
</style>
