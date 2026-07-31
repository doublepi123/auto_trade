<script setup lang="ts">
import { ref } from 'vue'
import { getEdgeQuality, type EdgeQualityResult } from '../api/edgeQuality'

const loading = ref(false)
const result = ref<EdgeQualityResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(180)

async function run() {
  loading.value = true
  try {
    result.value = await getEdgeQuality({
      symbol: symbol.value || undefined,
      lookback_days: lookbackDays.value,
    })
  } finally {
    loading.value = false
  }
}

function gradeColor(g: string): string {
  if (g === 'A') return '#67c23a'
  if (g === 'B') return '#409eff'
  if (g === 'C') return '#e6a23c'
  if (g === 'D') return '#f56c6c'
  return '#909399'
}
</script>

<template>
  <div class="page-container">
    <h2>优势质量评分</h2>
    <p class="page-desc">综合期望值、一致性、回撤控制与样本充分性的 0-100 策略质量分（灵感来自 Edgewonk / QuantStats）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="symbol" placeholder="全部" clearable style="width: 140px" />
        </el-form-item>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="7" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">评分</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="result">
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="8">
            <el-card shadow="hover" class="grade-card">
              <span class="grade-label">综合评分</span>
              <span class="grade-value" :style="{ color: gradeColor(result.grade) }">{{ result.composite_score }}</span>
              <el-tag :color="gradeColor(result.grade)" effect="dark" size="large" class="grade-tag">{{ result.grade }}</el-tag>
            </el-card>
          </el-col>
          <el-col :span="16">
            <el-card>
              <template #header>因子得分</template>
              <div class="factor-list">
                <div v-for="(factor, key) in result.factors" :key="key" class="factor-row">
                  <span class="factor-name">{{ key === 'expectancy' ? '期望值' : key === 'consistency' ? '一致性' : key === 'drawdown_control' ? '回撤控制' : '样本充分性' }}</span>
                  <el-progress :percentage="Math.round(factor.score / factor.max * 100)" :stroke-width="16" :format="() => `${factor.score}/${factor.max}`" />
                </div>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-alert :title="result.recommendation" type="info" :closable="false" style="margin-top: 16px" />

        <el-card style="margin-top: 16px">
          <template #header>底层指标</template>
          <el-descriptions :column="4" border size="small">
            <el-descriptions-item label="胜率">{{ (result.underlying.win_rate * 100).toFixed(1) }}%</el-descriptions-item>
            <el-descriptions-item label="期望值">{{ result.underlying.expectancy }}</el-descriptions-item>
            <el-descriptions-item label="盈亏比">{{ result.underlying.payoff_ratio ?? '-' }}</el-descriptions-item>
            <el-descriptions-item label="最大回撤">{{ result.underlying.max_drawdown }}</el-descriptions-item>
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
.grade-card { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 16px 0; }
.grade-label { font-size: 12px; color: #909399; }
.grade-value { font-size: 48px; font-weight: 700; }
.grade-tag { font-size: 20px; }
.factor-list { display: flex; flex-direction: column; gap: 12px; }
.factor-row { display: flex; align-items: center; gap: 12px; }
.factor-name { width: 80px; font-size: 13px; color: #606266; flex-shrink: 0; }
</style>
