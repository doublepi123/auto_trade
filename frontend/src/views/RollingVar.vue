<script setup lang="ts">
import { ref } from 'vue'
import { getRollingVar, type RollingVarResult } from '../api/rollingVar'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'

const loading = ref(false)
const result = ref<RollingVarResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(180)
const window = ref(30)

async function run() {
  loading.value = true
  try {
    result.value = await getRollingVar({
      symbol: symbol.value || undefined,
      lookback_days: lookbackDays.value,
      window: window.value,
    })
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="page-container">
    <h2>滚动 VaR/CVaR</h2>
    <p class="page-desc">滑动窗口历史 Value-at-Risk 与条件 VaR（Expected Shortfall）（灵感来自 VectorBT / QuantStats）</p>

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
      <StatisticsQualityAlert :quality="result.statistics_quality" style="margin-top: 16px" />
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="最新 VaR" :value="result.summary.latest_var" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="最新 CVaR" :value="result.summary.latest_cvar" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="VaR 均值" :value="result.summary.var_mean" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="CVaR 均值" :value="result.summary.cvar_mean" :precision="2" />
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>VaR / CVaR 序列（最近 50 个窗口）</template>
          <div class="spark-row">
            <div
              v-for="(pt, i) in result.points"
              :key="i"
              class="spark-bar"
              :style="{ height: Math.min(pt.cvar * 3, 60) + 'px' }"
              :title="`#${pt.index} VaR=${pt.var} CVaR=${pt.cvar}`"
            />
          </div>
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>风险指标汇总</template>
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="VaR 最大">{{ result.summary.var_max }}</el-descriptions-item>
            <el-descriptions-item label="CVaR 最大">{{ result.summary.cvar_max }}</el-descriptions-item>
            <el-descriptions-item label="置信度">{{ (result.confidence * 100).toFixed(0) }}%</el-descriptions-item>
            <el-descriptions-item label="窗口大小">{{ result.window }}</el-descriptions-item>
            <el-descriptions-item label="样本数">{{ result.sample_size }}</el-descriptions-item>
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
.spark-row { display: flex; align-items: flex-end; gap: 2px; height: 70px; }
.spark-bar { width: 8px; border-radius: 2px 2px 0 0; min-height: 2px; background: #f56c6c; }
</style>
