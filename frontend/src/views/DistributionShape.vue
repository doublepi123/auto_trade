<script setup lang="ts">
import { ref } from 'vue'
import { getDistributionShape, type DistributionShapeResult } from '../api/distributionShape'

const loading = ref(false)
const result = ref<DistributionShapeResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(180)

async function run() {
  loading.value = true
  try {
    result.value = await getDistributionShape({
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
    <h2>PnL 分布形态</h2>
    <p class="page-desc">偏度、峰度与正态性检验，刻画收益分布的尾部风险（灵感来自 VectorBT / QuantStats）</p>

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
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="偏度 (Skewness)" :value="result.skewness" :precision="4" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="超额峰度 (Kurtosis)" :value="result.kurtosis" :precision="4" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="Jarque-Bera" :value="result.jarque_bera" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">正态性</span>
                <el-tag :type="result.is_normal_like ? 'success' : 'danger'" size="large">
                  {{ result.is_normal_like ? '近似正态' : '非正态' }}
                </el-tag>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="12">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">尾部特征</span>
                <span class="stat-value">{{ result.tail_label }}</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="12">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">对称性</span>
                <span class="stat-value">{{ result.asymmetry }}</span>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-alert :title="result.interpretation" type="info" :closable="false" style="margin-top: 16px" />

        <el-card style="margin-top: 16px">
          <template #header>分位数与基础统计</template>
          <el-descriptions :column="4" border size="small">
            <el-descriptions-item label="均值">{{ result.mean }}</el-descriptions-item>
            <el-descriptions-item label="标准差">{{ result.std }}</el-descriptions-item>
            <el-descriptions-item label="IQR">{{ result.iqr }}</el-descriptions-item>
            <el-descriptions-item label="样本数">{{ result.sample_size }}</el-descriptions-item>
            <el-descriptions-item label="P5">{{ result.percentiles.p5 }}</el-descriptions-item>
            <el-descriptions-item label="P25">{{ result.percentiles.p25 }}</el-descriptions-item>
            <el-descriptions-item label="P50">{{ result.percentiles.p50 }}</el-descriptions-item>
            <el-descriptions-item label="P75">{{ result.percentiles.p75 }}</el-descriptions-item>
            <el-descriptions-item label="P95">{{ result.percentiles.p95 }}</el-descriptions-item>
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
.stat-custom { display: flex; flex-direction: column; align-items: center; gap: 6px; }
.stat-label { font-size: 12px; color: #909399; }
.stat-value { font-size: 20px; font-weight: 600; }
</style>
