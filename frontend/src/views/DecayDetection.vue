<script setup lang="ts">
import { ref } from 'vue'
import { getDecayDetection, type DecayDetectionResult } from '../api/decayDetection'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'

const loading = ref(false)
const result = ref<DecayDetectionResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(365)
const nWindows = ref(4)

async function run() {
  loading.value = true
  try {
    result.value = await getDecayDetection({
      symbol: symbol.value || undefined,
      lookback_days: lookbackDays.value,
      n_windows: nWindows.value,
    })
  } finally {
    loading.value = false
  }
}

function verdictType(v: string): 'success' | 'warning' | 'danger' {
  if (v === 'stable') return 'success'
  if (v === 'early-warning') return 'warning'
  return 'danger'
}

const verdictLabel: Record<string, string> = {
  stable: '稳定',
  'early-warning': '早期预警',
  decaying: '衰减中',
}

function pnlColor(v: number): string {
  return v > 0 ? '#67c23a' : v < 0 ? '#f56c6c' : '#909399'
}
</script>

<template>
  <div class="page-container">
    <h2>策略衰减检测</h2>
    <p class="page-desc">将交易历史切分为顺序窗口，检测胜率/Sharpe/期望值是否随时间退化（灵感来自 QuantStats / VectorBT）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="symbol" placeholder="全部" clearable style="width: 140px" />
        </el-form-item>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="30" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item label="窗口数">
          <el-input-number v-model="nWindows" :min="2" :max="10" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">检测</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="result">
      <StatisticsQualityAlert :quality="result.statistics_quality" />
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="8">
            <el-card shadow="hover" class="verdict-card">
              <span class="verdict-label">衰减判定</span>
              <el-tag :type="verdictType(result.verdict)" size="large" effect="dark">
                {{ verdictLabel[result.verdict] ?? result.verdict }}
              </el-tag>
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="衰减信号数" :value="result.decay_signals" suffix="/ 3" />
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="样本数" :value="result.sample_size" />
            </el-card>
          </el-col>
        </el-row>

        <el-alert :title="result.assessment" :type="verdictType(result.verdict)" :closable="false" style="margin-top: 16px" />

        <el-card style="margin-top: 16px">
          <template #header>窗口趋势</template>
          <el-table :data="result.windows" size="small">
            <el-table-column prop="window" label="窗口" width="60" />
            <el-table-column prop="trade_count" label="交易数" width="80" />
            <el-table-column prop="win_rate" label="胜率" width="100">
              <template #default="{ row }">{{ (row.win_rate * 100).toFixed(1) }}%</template>
            </el-table-column>
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
            <el-table-column prop="sharpe" label="Sharpe" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.sharpe) }">{{ row.sharpe }}</span>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>斜率（每窗口变化）</template>
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="胜率斜率">
              <span :style="{ color: pnlColor(result.slopes.win_rate_per_window) }">{{ result.slopes.win_rate_per_window }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="Sharpe 斜率">
              <span :style="{ color: pnlColor(result.slopes.sharpe_per_window) }">{{ result.slopes.sharpe_per_window }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="平均 PnL 斜率">
              <span :style="{ color: pnlColor(result.slopes.avg_pnl_per_window) }">{{ result.slopes.avg_pnl_per_window }}</span>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </template>
    </template>
  </div>
</template>

<style scoped>
.page-container { padding: 20px; }
.page-desc { color: #909399; margin-bottom: 16px; }
.control-card { margin-bottom: 8px; }
.verdict-card { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 12px 0; }
.verdict-label { font-size: 12px; color: #909399; }
</style>
