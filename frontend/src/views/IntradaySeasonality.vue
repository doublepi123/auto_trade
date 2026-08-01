<script setup lang="ts">
import { ref } from 'vue'
import { getIntradaySeasonality, type IntradaySeasonalityResult } from '../api/intradaySeasonality'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'

const loading = ref(false)
const result = ref<IntradaySeasonalityResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(180)

async function run() {
  loading.value = true
  try {
    result.value = await getIntradaySeasonality({
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
</script>

<template>
  <div class="page-container">
    <h2>日内季节性</h2>
    <p class="page-desc">按 30 分钟时段统计平均 PnL，发现盘中时间优势（灵感来自 Freqtrade / QuantStats）</p>

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
        <el-row :gutter="16" style="margin-top: 16px" v-if="result.best_bucket">
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="样本数" :value="result.sample_size" />
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">最佳时段</span>
                <span class="stat-value text-green">{{ result.best_bucket.bucket }}</span>
                <span class="stat-sub">{{ result.best_bucket.avg_pnl }} avg</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover" v-if="result.worst_bucket">
              <div class="stat-custom">
                <span class="stat-label">最差时段</span>
                <span class="stat-value text-red">{{ result.worst_bucket.bucket }}</span>
                <span class="stat-sub">{{ result.worst_bucket.avg_pnl }} avg</span>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>30 分钟时段 PnL</template>
          <el-table :data="result.buckets" size="small">
            <el-table-column prop="bucket" label="时段" width="120" />
            <el-table-column prop="trade_count" label="交易数" width="80" />
            <el-table-column prop="avg_pnl" label="平均 PnL" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.avg_pnl) }">{{ row.avg_pnl }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="total_pnl" label="总 PnL" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.total_pnl) }">{{ row.total_pnl }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="win_rate" label="胜率" width="80">
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
.stat-custom { display: flex; flex-direction: column; align-items: center; gap: 4px; }
.stat-label { font-size: 12px; color: #909399; }
.stat-value { font-size: 20px; font-weight: 600; }
.stat-sub { font-size: 13px; color: #606266; }
.text-green { color: #67c23a; }
.text-red { color: #f56c6c; }
</style>
