<script setup lang="ts">
import { ref } from 'vue'
import { getRiskScore, type RiskScoreResult } from '../api/riskScore'

const loading = ref(false)
const result = ref<RiskScoreResult | null>(null)
const lookbackDays = ref(180)

async function run() {
  loading.value = true
  try {
    result.value = await getRiskScore({ lookback_days: lookbackDays.value })
  } finally {
    loading.value = false
  }
}

function riskTagType(level: string): 'danger' | 'warning' | 'success' {
  if (level === 'high') return 'danger'
  if (level === 'medium') return 'warning'
  return 'success'
}

function scoreColor(v: number): string {
  if (v > 60) return '#f56c6c'
  if (v > 35) return '#e6a23c'
  return '#67c23a'
}
</script>

<template>
  <div class="page-container">
    <h2>综合风险评分</h2>
    <p class="page-desc">多因子风险评分：波动率 + 回撤 + 连败 + 亏损比率 → 0-100 综合分（灵感来自 QuantConnect / Lean）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="7" :max="3650" :step="30" />
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
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="标的数" :value="result.symbols.length" />
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="样本交易数" :value="result.sample_size" />
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="平均综合分" :value="result.avg_composite" :precision="2" />
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>标的风险排名</template>
          <el-table :data="result.symbols" size="small" max-height="500">
            <el-table-column prop="symbol" label="标的" width="120" />
            <el-table-column prop="composite_score" label="综合分" width="100">
              <template #default="{ row }">
                <span :style="{ color: scoreColor(row.composite_score), fontWeight: 700 }">
                  {{ row.composite_score }}
                </span>
              </template>
            </el-table-column>
            <el-table-column prop="risk_level" label="风险等级" width="100">
              <template #default="{ row }">
                <el-tag :type="riskTagType(row.risk_level)" size="small">{{ row.risk_level }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="trade_count" label="交易数" width="80" />
            <el-table-column prop="total_pnl" label="总 PnL" width="100" />
            <el-table-column prop="max_drawdown" label="最大回撤" width="100" />
            <el-table-column prop="max_loss_streak" label="最长连败" width="90" />
            <el-table-column label="因子分" min-width="200">
              <template #default="{ row }">
                <div class="factor-row">
                  <span title="波动率">V:{{ row.factor_scores.volatility.toFixed(0) }}</span>
                  <span title="回撤">D:{{ row.factor_scores.drawdown.toFixed(0) }}</span>
                  <span title="连败">S:{{ row.factor_scores.streak.toFixed(0) }}</span>
                  <span title="亏损率">L:{{ row.factor_scores.loss_ratio.toFixed(0) }}</span>
                </div>
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
.factor-row { display: flex; gap: 12px; font-size: 12px; font-variant-numeric: tabular-nums; }
</style>
