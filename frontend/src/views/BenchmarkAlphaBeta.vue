<script setup lang="ts">
import { ref } from 'vue'
import { getBenchmarkAlphaBeta, type BenchmarkResult } from '../api/benchmark'

const loading = ref(false)
const result = ref<BenchmarkResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(180)

async function run() {
  loading.value = true
  try {
    result.value = await getBenchmarkAlphaBeta({
      symbol: symbol.value || undefined,
      lookback_days: lookbackDays.value,
    })
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="page-container">
    <h2>基准 Alpha/Beta</h2>
    <p class="page-desc">策略日 PnL 对内部市场代理的 OLS 回归，估算系统性暴露与特异收益（灵感来自 Zipline / QuantConnect）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="symbol" placeholder="全部" clearable style="width: 140px" />
        </el-form-item>
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
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="Alpha (日)" :value="result.alpha" :precision="4" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="Beta" :value="result.beta" :precision="4" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="R²" :value="result.r_squared" :precision="4" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="信息比率" :value="result.information_ratio" :precision="4" />
            </el-card>
          </el-col>
        </el-row>

        <el-alert
          :title="result.interpretation"
          type="info"
          :closable="false"
          style="margin-top: 16px"
        />

        <el-card style="margin-top: 16px">
          <template #header>回归详情</template>
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="交易日数">{{ result.trading_days }}</el-descriptions-item>
            <el-descriptions-item label="样本交易数">{{ result.sample_size }}</el-descriptions-item>
            <el-descriptions-item label="回看天数">{{ result.lookback_days }}</el-descriptions-item>
            <el-descriptions-item label="市场日均 PnL">{{ result.market_mean_daily }}</el-descriptions-item>
            <el-descriptions-item label="策略日均 PnL">{{ result.strategy_mean_daily }}</el-descriptions-item>
            <el-descriptions-item label="标的">{{ result.symbol }}</el-descriptions-item>
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
