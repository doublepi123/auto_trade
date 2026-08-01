<script setup lang="ts">
import { ref } from 'vue'
import { getHoldingTimeAnalysis, type HoldingTimeResult } from '../api/holdingTime'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'

const loading = ref(false)
const result = ref<HoldingTimeResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(180)

async function run() {
  loading.value = true
  try {
    result.value = await getHoldingTimeAnalysis({
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

function fmtDuration(s: number): string {
  if (s < 60) return `${s.toFixed(0)}s`
  if (s < 3600) return `${(s / 60).toFixed(1)}m`
  return `${(s / 3600).toFixed(1)}h`
}
</script>

<template>
  <div class="page-container">
    <h2>持仓时长分析</h2>
    <p class="page-desc">按持仓时长分桶统计 PnL，揭示短持与长持的收益差异（灵感来自 Freqtrade）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="symbol" placeholder="全部" clearable style="width: 140px" />
        </el-form-item>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="7" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">分析</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="result">
      <StatisticsQualityAlert :quality="result.statistics_quality" />
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="样本数" :value="result.sample_size" />
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">平均持仓</span>
                <span class="stat-value">{{ fmtDuration(result.avg_holding_seconds) }}</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">中位持仓</span>
                <span class="stat-value">{{ fmtDuration(result.median_holding_seconds) }}</span>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-alert
          v-if="result.best_bucket"
          :title="`最佳时长桶: ${result.best_bucket.bucket}（平均 PnL ${result.best_bucket.avg_pnl}，胜率 ${(result.best_bucket.win_rate * 100).toFixed(1)}%）`"
          type="success"
          :closable="false"
          style="margin-top: 16px"
        />

        <el-card style="margin-top: 16px">
          <template #header>时长桶 PnL 分布</template>
          <el-table :data="result.buckets" size="small">
            <el-table-column prop="bucket" label="时长区间" width="100" />
            <el-table-column prop="trade_count" label="交易数" width="80" />
            <el-table-column prop="total_pnl" label="总 PnL" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.total_pnl) }">{{ row.total_pnl }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="avg_pnl" label="平均 PnL" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.avg_pnl) }">{{ row.avg_pnl }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="win_rate" label="胜率" width="100">
              <template #default="{ row }">{{ (row.win_rate * 100).toFixed(1) }}%</template>
            </el-table-column>
            <el-table-column label="分布">
              <template #default="{ row }">
                <el-progress
                  v-if="row.trade_count > 0"
                  :percentage="Math.min(row.trade_count * 5, 100)"
                  :show-text="false"
                  :stroke-width="12"
                  :color="pnlColor(row.avg_pnl)"
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
.stat-value { font-size: 24px; font-weight: 600; margin-top: 4px; }
</style>
