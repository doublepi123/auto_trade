<script setup lang="ts">
import { ref } from 'vue'
import { getConcentration, type ConcentrationResult } from '../api/concentration'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'

const loading = ref(false)
const result = ref<ConcentrationResult | null>(null)
const lookbackDays = ref(180)

async function run() {
  loading.value = true
  try {
    result.value = await getConcentration({ lookback_days: lookbackDays.value })
  } finally {
    loading.value = false
  }
}

function levelType(l: string): 'danger' | 'warning' | 'success' | 'info' {
  if (l === 'high') return 'danger'
  if (l === 'moderate') return 'warning'
  if (l === 'unavailable') return 'info'
  return 'success'
}

function levelLabel(level: string): string {
  if (level === 'unavailable') return '不可用'
  if (level === 'high') return '高'
  if (level === 'moderate') return '中'
  return '低'
}
</script>

<template>
  <div class="page-container">
    <h2>标的集中度</h2>
    <p class="page-desc">HHI 与有效 N 度量集中度；所有标的净 PnL 都为零时，PnL 集中度明确标记为不可用</p>

    <el-card class="control-card">
      <el-form inline>
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
        <el-alert :title="result.assessment" :type="levelType(result.concentration_level)" :closable="false" style="margin-top: 16px" />

        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="标的数" :value="result.symbol_count" />
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic v-if="result.hhi_pnl !== null" title="HHI (PnL)" :value="result.hhi_pnl" :precision="4" />
              <div v-else class="stat-custom">
                <span class="stat-label">HHI (PnL)</span>
                <span class="stat-value">不可用</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic v-if="result.effective_n_pnl !== null" title="有效 N (PnL)" :value="result.effective_n_pnl" :precision="2" />
              <div v-else class="stat-custom">
                <span class="stat-label">有效 N (PnL)</span>
                <span class="stat-value">不可用</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="HHI (次数)" :value="result.hhi_count" :precision="4" />
            </el-card>
          </el-col>
          <el-col :span="4">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">集中度</span>
                <el-tag :type="levelType(result.concentration_level)" size="large">{{ levelLabel(result.concentration_level) }}</el-tag>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>标的 PnL 份额</template>
          <el-table :data="result.breakdown" size="small" max-height="400">
            <el-table-column prop="symbol" label="标的" width="120" />
            <el-table-column prop="trade_count" label="交易数" width="80" />
            <el-table-column prop="total_pnl" label="总 PnL" width="100" />
            <el-table-column prop="pnl_share" label="PnL 份额" width="120">
              <template #default="{ row }">
                <el-progress v-if="row.pnl_share !== null" :percentage="Math.round(row.pnl_share * 100)" :stroke-width="12" />
                <span v-else>不可用</span>
              </template>
            </el-table-column>
            <el-table-column prop="count_share" label="次数份额" width="120">
              <template #default="{ row }">{{ (row.count_share * 100).toFixed(1) }}%</template>
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
.stat-custom { display: flex; flex-direction: column; align-items: center; gap: 6px; }
.stat-label { font-size: 12px; color: #909399; }
</style>
