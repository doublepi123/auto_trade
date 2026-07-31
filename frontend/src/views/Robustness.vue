<script setup lang="ts">
import { ref } from 'vue'
import { getRobustness, type RobustnessResult } from '../api/robustness'

const loading = ref(false)
const result = ref<RobustnessResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(365)

async function run() {
  loading.value = true
  try {
    result.value = await getRobustness({
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

const factorLabels: Record<string, string> = {
  sub_period_stability: '子周期稳定性',
  outlier_independence: '离群值独立性',
  wr_consistency: '胜率一致性',
  sample_adequacy: '样本充分性',
}
</script>

<template>
  <div class="page-container">
    <h2>策略稳健性指数</h2>
    <p class="page-desc">通过子周期稳定性、离群值依赖度和胜率一致性评估策略边缘的可靠性（灵感来自 QuantStats / VectorBT）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="symbol" placeholder="全部" clearable style="width: 140px" />
        </el-form-item>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="30" :max="3650" :step="30" />
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
              <span class="grade-label">稳健性评分</span>
              <span class="grade-value" :style="{ color: gradeColor(result.grade) }">{{ result.composite_score }}</span>
              <el-tag :color="gradeColor(result.grade)" effect="dark" size="large">{{ result.grade }}</el-tag>
            </el-card>
          </el-col>
          <el-col :span="16">
            <el-card>
              <template #header>因子得分</template>
              <div class="factor-list">
                <div v-for="(factor, key) in result.factors" :key="key" class="factor-row">
                  <span class="factor-name">{{ factorLabels[key] ?? key }}</span>
                  <el-progress :percentage="Math.round(factor.score / factor.max * 100)" :stroke-width="16" :format="() => `${factor.score}/${factor.max}`" />
                </div>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-alert :title="result.recommendation" type="info" :closable="false" style="margin-top: 16px" />

        <el-card style="margin-top: 16px">
          <template #header>季度 PnL</template>
          <div class="quarter-row">
            <div v-for="(qp, i) in result.quarter_pnls" :key="i" class="quarter-item">
              <span class="q-label">Q{{ i + 1 }}</span>
              <span class="q-value" :style="{ color: qp > 0 ? '#67c23a' : '#f56c6c' }">{{ qp }}</span>
            </div>
          </div>
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
.factor-list { display: flex; flex-direction: column; gap: 12px; }
.factor-row { display: flex; align-items: center; gap: 12px; }
.factor-name { width: 100px; font-size: 13px; color: #606266; flex-shrink: 0; }
.quarter-row { display: flex; gap: 24px; }
.quarter-item { display: flex; flex-direction: column; align-items: center; gap: 4px; }
.q-label { font-size: 12px; color: #909399; }
.q-value { font-size: 18px; font-weight: 600; }
</style>
