<script setup lang="ts">
import { ref } from 'vue'
import { getRegimeSensitivity, type RegimeSensitivityResult } from '../api/regimeSensitivity'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'

const loading = ref(false)
const result = ref<RegimeSensitivityResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(180)
const window = ref(20)

async function run() {
  loading.value = true
  try {
    result.value = await getRegimeSensitivity({
      symbol: symbol.value || undefined,
      lookback_days: lookbackDays.value,
      window: window.value,
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
    <h2>策略历史结果波动状态敏感性</h2>
    <p class="page-desc">仅用入场前已平仓交易的 PnL 波动划分高/低状态；这是策略结果状态，不是市场波动率信号</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="symbol" placeholder="全部" clearable style="width: 140px" />
        </el-form-item>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="7" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item label="窗口">
          <el-input-number v-model="window" :min="5" :max="100" :step="5" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">分析</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="result">
      <StatisticsQualityAlert :quality="result.statistics_quality" style="margin-top: 16px" />
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-alert :title="result.interpretation" type="info" :closable="false" style="margin-top: 16px" />

        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="敏感度 (Sharpe 差)" :value="result.sensitivity" :precision="4" />
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="历史 PnL 中位波动" :value="result.median_volatility" :precision="4" />
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="样本数" :value="result.sample_size" />
            </el-card>
          </el-col>
        </el-row>

        <el-row :gutter="16" style="margin-top: 16px">
          <el-col v-for="regime in result.regimes" :key="regime.regime" :span="12">
            <el-card>
              <template #header>{{ regime.regime === 'low_volatility' ? '此前 PnL 低波动状态' : '此前 PnL 高波动状态' }}</template>
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="交易数">{{ regime.trade_count }}</el-descriptions-item>
                <el-descriptions-item label="胜率">{{ (regime.win_rate * 100).toFixed(1) }}%</el-descriptions-item>
                <el-descriptions-item label="平均 PnL">
                  <span :style="{ color: pnlColor(regime.avg_pnl) }">{{ regime.avg_pnl }}</span>
                </el-descriptions-item>
                <el-descriptions-item label="总 PnL">
                  <span :style="{ color: pnlColor(regime.total_pnl) }">{{ regime.total_pnl }}</span>
                </el-descriptions-item>
                <el-descriptions-item label="Sharpe">
                  <span :style="{ color: pnlColor(regime.sharpe) }">{{ regime.sharpe }}</span>
                </el-descriptions-item>
              </el-descriptions>
            </el-card>
          </el-col>
        </el-row>
      </template>
    </template>
  </div>
</template>

<style scoped>
.page-container { padding: 20px; }
.page-desc { color: #909399; margin-bottom: 16px; }
.control-card { margin-bottom: 8px; }
</style>
