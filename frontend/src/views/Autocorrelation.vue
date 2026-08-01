<script setup lang="ts">
import { ref } from 'vue'
import { getAutocorrelation, type AutocorrelationResult } from '../api/autocorrelation'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'

const loading = ref(false)
const result = ref<AutocorrelationResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(180)
const maxLag = ref(10)

async function run() {
  loading.value = true
  try {
    result.value = await getAutocorrelation({
      symbol: symbol.value || undefined,
      lookback_days: lookbackDays.value,
      max_lag: maxLag.value,
    })
  } finally {
    loading.value = false
  }
}

const patternLabel: Record<string, string> = {
  independent: '独立（无序列依赖）',
  momentum: '动量（赢后继续赢）',
  'mean-reversion': '均值回归（赢后易亏）',
  'weak-dependence': '弱依赖',
}

function patternType(p: string): 'success' | 'warning' | 'info' | 'danger' {
  if (p === 'independent') return 'success'
  if (p === 'momentum') return 'warning'
  if (p === 'mean-reversion') return 'danger'
  return 'info'
}
</script>

<template>
  <div class="page-container">
    <h2>PnL 自相关</h2>
    <p class="page-desc">检测交易收益序列的动量或均值回归模式；零方差样本会明确标记为退化，不解释为独立</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="symbol" placeholder="全部" clearable style="width: 140px" />
        </el-form-item>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="7" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item label="最大滞后">
          <el-input-number v-model="maxLag" :min="1" :max="50" />
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
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">模式</span>
                <el-tag :type="patternType(result.pattern)" size="large">{{ patternLabel[result.pattern] ?? result.pattern }}</el-tag>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="Ljung-Box Q" :value="result.ljung_box_q" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="显著滞后数" :value="result.significant_lags" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="置信带" :value="result.confidence_band" :precision="4" />
            </el-card>
          </el-col>
        </el-row>

        <el-alert :title="result.interpretation" type="info" :closable="false" style="margin-top: 16px" />

        <el-card style="margin-top: 16px">
          <template #header>ACF 图（±置信带 {{ result.confidence_band }}）</template>
          <div class="acf-chart">
            <div v-for="lag in result.lags" :key="lag.lag" class="acf-bar-group">
              <div class="acf-bar-wrapper">
                <div
                  class="acf-bar"
                  :class="[lag.acf >= 0 ? 'bar-pos' : 'bar-neg', lag.significant ? 'sig' : '']"
                  :style="{ height: Math.min(Math.abs(lag.acf) * 150, 60) + 'px' }"
                  :title="`lag=${lag.lag} acf=${lag.acf}`"
                />
              </div>
              <span class="acf-label">{{ lag.lag }}</span>
            </div>
          </div>
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>滞后明细</template>
          <el-table :data="result.lags" size="small">
            <el-table-column prop="lag" label="Lag" width="60" />
            <el-table-column prop="acf" label="ACF" width="100" />
            <el-table-column label="显著性" width="100">
              <template #default="{ row }">
                <el-tag :type="row.significant ? 'danger' : 'info'" size="small">
                  {{ row.significant ? '显著' : '不显著' }}
                </el-tag>
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
.stat-custom { display: flex; flex-direction: column; align-items: center; gap: 6px; }
.stat-label { font-size: 12px; color: #909399; }
.acf-chart { display: flex; align-items: flex-end; gap: 6px; height: 80px; padding-top: 10px; }
.acf-bar-group { display: flex; flex-direction: column; align-items: center; }
.acf-bar-wrapper { display: flex; align-items: flex-end; height: 60px; }
.acf-bar { width: 16px; border-radius: 2px 2px 0 0; min-height: 2px; }
.bar-pos { background: #409eff; }
.bar-neg { background: #f56c6c; }
.sig { opacity: 1; }
.acf-bar:not(.sig) { opacity: 0.4; }
.acf-label { font-size: 10px; color: #909399; margin-top: 4px; }
</style>
