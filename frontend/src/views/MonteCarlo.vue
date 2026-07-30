<script setup lang="ts">
import { ref } from 'vue'
import { getMonteCarloSimulation, type MonteCarloResult } from '../api/monteCarlo'

const loading = ref(false)
const result = ref<MonteCarloResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(180)
const nSimulations = ref(1000)

async function run() {
  loading.value = true
  try {
    result.value = await getMonteCarloSimulation({
      symbol: symbol.value || undefined,
      lookback_days: lookbackDays.value,
      n_simulations: nSimulations.value,
    })
  } finally {
    loading.value = false
  }
}

function pct(v: number): string {
  return (v * 100).toFixed(1) + '%'
}
</script>

<template>
  <div class="page-container">
    <h2>蒙特卡洛模拟</h2>
    <p class="page-desc">基于历史成交 PnL 的 Bootstrap 重采样，估算未来收益分布与破产概率（灵感来自 QuantStats）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="symbol" placeholder="全部" clearable style="width: 140px" />
        </el-form-item>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="30" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item label="模拟次数">
          <el-input-number v-model="nSimulations" :min="100" :max="50000" :step="500" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">运行模拟</el-button>
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
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="样本交易数" :value="result.sample_size" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="盈利概率" :value="result.profit_probability * 100" :precision="1" suffix="%" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="破产概率" :value="result.ruin_probability * 100" :precision="2" suffix="%" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="样本胜率" :value="result.sample_stats.win_rate * 100" :precision="1" suffix="%" />
            </el-card>
          </el-col>
        </el-row>

        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="12">
            <el-card>
              <template #header>终值 PnL 分布</template>
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="均值">{{ result.final_pnl_distribution.mean }}</el-descriptions-item>
                <el-descriptions-item label="中位数">{{ result.final_pnl_distribution.median }}</el-descriptions-item>
                <el-descriptions-item label="P5">{{ result.final_pnl_distribution.p5 }}</el-descriptions-item>
                <el-descriptions-item label="P25">{{ result.final_pnl_distribution.p25 }}</el-descriptions-item>
                <el-descriptions-item label="P75">{{ result.final_pnl_distribution.p75 }}</el-descriptions-item>
                <el-descriptions-item label="P95">{{ result.final_pnl_distribution.p95 }}</el-descriptions-item>
                <el-descriptions-item label="最小">{{ result.final_pnl_distribution.min }}</el-descriptions-item>
                <el-descriptions-item label="最大">{{ result.final_pnl_distribution.max }}</el-descriptions-item>
              </el-descriptions>
            </el-card>
          </el-col>
          <el-col :span="12">
            <el-card>
              <template #header>最大回撤分布</template>
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="均值">{{ result.max_drawdown_distribution.mean }}</el-descriptions-item>
                <el-descriptions-item label="中位数">{{ result.max_drawdown_distribution.median }}</el-descriptions-item>
                <el-descriptions-item label="P95">{{ result.max_drawdown_distribution.p95 }}</el-descriptions-item>
                <el-descriptions-item label="最大">{{ result.max_drawdown_distribution.max }}</el-descriptions-item>
              </el-descriptions>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>样本统计</template>
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="平均 PnL">{{ result.sample_stats.mean_pnl }}</el-descriptions-item>
            <el-descriptions-item label="总 PnL">{{ result.sample_stats.total_pnl }}</el-descriptions-item>
            <el-descriptions-item label="胜率">{{ pct(result.sample_stats.win_rate) }}</el-descriptions-item>
            <el-descriptions-item label="最佳交易">{{ result.sample_stats.best_trade }}</el-descriptions-item>
            <el-descriptions-item label="最差交易">{{ result.sample_stats.worst_trade }}</el-descriptions-item>
            <el-descriptions-item label="模拟轮数">{{ result.n_simulations }}</el-descriptions-item>
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
</style>
