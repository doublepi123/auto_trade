<script setup lang="ts">
import { computed, ref } from 'vue'
import { getScratchAnalysisSummary, type ScratchAnalysisResult } from '../api/scratchAnalysis'

const loading = ref(false)
const result = ref<ScratchAnalysisResult | null>(null)
const days = ref(90)

async function run() {
  loading.value = true
  try {
    result.value = await getScratchAnalysisSummary({ days: days.value })
  } finally {
    loading.value = false
  }
}

const maxRate = computed(() => {
  if (!result.value?.weekly?.length) return 0
  return Math.max(...result.value.weekly.map((w) => w.scratch_rate))
})

function min(v: number | null): string {
  return v != null ? v.toFixed(1) + 'm' : '—'
}
</script>

<template>
  <div class="page-container">
    <h2>保本交易分析</h2>
    <p class="page-desc">净盈亏未超过交易成本的「擦边」交易占比、持仓时间与趋势（灵感来自 Edgewonk scratch-trade 日志）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="回看天数">
          <el-input-number v-model="days" :min="7" :max="3650" :step="30" />
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
              <div class="stat-custom">
                <span class="stat-label">保本率</span>
                <span class="stat-value">{{ (result.scratch_rate * 100).toFixed(1) }}%</span>
                <span class="stat-sub">{{ result.scratch_count }} / {{ result.sample_size }} 笔</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="保本单费用合计" :value="result.scratch_fee_total" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">保本单平均持仓</span>
                <span class="stat-value">{{ min(result.avg_scratch_hold_min) }}</span>
                <span class="stat-sub">中位 {{ min(result.median_scratch_hold_min) }}</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">有效单平均持仓</span>
                <span class="stat-value">{{ min(result.avg_decisive_hold_min) }}</span>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px" v-if="result.weekly.length">
          <template #header>每周保本率</template>
          <div class="bar-chart">
            <div v-for="w in result.weekly" :key="w.week" class="bar-item" :title="`${w.week}: ${(w.scratch_rate * 100).toFixed(1)}% (${w.total} 笔)`">
              <div class="bar" :style="{ height: maxRate > 0 ? (w.scratch_rate / maxRate) * 100 + '%' : '0%' }" />
              <span class="bar-label">{{ w.week }}</span>
            </div>
          </div>
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>分标的</template>
          <el-table :data="result.by_symbol" size="small" max-height="480">
            <el-table-column prop="symbol" label="标的" width="120" />
            <el-table-column prop="total" label="总笔数" width="90" />
            <el-table-column prop="scratch" label="保本笔数" width="90" />
            <el-table-column label="保本率">
              <template #default="{ row }">
                <div class="rate-wrap">
                  <div class="rate-bar" :style="{ width: (row.scratch_rate * 100).toFixed(1) + '%' }" />
                  <span>{{ (row.scratch_rate * 100).toFixed(1) }}%</span>
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
.stat-custom { display: flex; flex-direction: column; align-items: center; gap: 4px; }
.stat-label { font-size: 12px; color: #909399; }
.stat-value { font-size: 20px; font-weight: 600; }
.stat-sub { font-size: 12px; color: #606266; }
.bar-chart { display: flex; align-items: flex-end; gap: 4px; height: 140px; overflow-x: auto; }
.bar-item { display: flex; flex-direction: column; align-items: center; min-width: 44px; height: 100%; justify-content: flex-end; }
.bar { width: 28px; background: #e6a23c; border-radius: 2px 2px 0 0; }
.bar-label { font-size: 9px; color: #909399; white-space: nowrap; margin-top: 4px; }
.rate-wrap { display: flex; align-items: center; gap: 8px; }
.rate-bar { height: 12px; background: #e6a23c; border-radius: 2px; min-width: 1px; }
</style>
