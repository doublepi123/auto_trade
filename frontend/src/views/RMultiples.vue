<script setup lang="ts">
import { computed, ref } from 'vue'
import { getRMultiplesDistribution, type RMultiplesResult } from '../api/rMultiples'

const loading = ref(false)
const result = ref<RMultiplesResult | null>(null)
const days = ref(90)

async function run() {
  loading.value = true
  try {
    result.value = await getRMultiplesDistribution({ days: days.value })
  } finally {
    loading.value = false
  }
}

const maxCount = computed(() => {
  if (!result.value?.histogram?.length) return 0
  return Math.max(...result.value.histogram.map((b) => b.count))
})

function bucketColor(bucket: string): string {
  return bucket.startsWith('-') || bucket.startsWith('<') ? '#f56c6c' : '#67c23a'
}
</script>

<template>
  <div class="page-container">
    <h2>R 倍数分布</h2>
    <p class="page-desc">以平均亏损为 1R 风险单位归一化每笔净盈亏的分布（灵感来自 Edgewonk / QuantStats）</p>

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
                <span class="stat-label">期望值 (R)</span>
                <span class="stat-value" :style="{ color: result.expectancy_r > 0 ? '#67c23a' : '#f56c6c' }">
                  {{ result.expectancy_r.toFixed(3) }}
                </span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="1R 风险单位" :value="result.risk_unit" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">≥ +1R 占比</span>
                <span class="stat-value text-green">{{ (result.pct_ge_1r * 100).toFixed(1) }}%</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">≤ -1R 占比</span>
                <span class="stat-value text-red">{{ (result.pct_le_minus_1r * 100).toFixed(1) }}%</span>
                <span class="stat-sub">范围 {{ result.min_r }}R ~ {{ result.max_r }}R</span>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>R 倍数直方图</template>
          <div class="hist">
            <div v-for="b in result.histogram" :key="b.bucket" class="hist-item" :title="`${b.bucket}: ${b.count}`">
              <span class="hist-count">{{ b.count || '' }}</span>
              <div
                class="hist-bar"
                :style="{
                  height: maxCount > 0 ? (b.count / maxCount) * 100 + '%' : '0%',
                  background: bucketColor(b.bucket),
                }"
              />
              <span class="hist-label">{{ b.bucket }}</span>
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
.stat-custom { display: flex; flex-direction: column; align-items: center; gap: 4px; }
.stat-label { font-size: 12px; color: #909399; }
.stat-value { font-size: 20px; font-weight: 600; }
.stat-sub { font-size: 12px; color: #606266; }
.text-green { color: #67c23a; }
.text-red { color: #f56c6c; }
.hist { display: flex; align-items: flex-end; gap: 8px; height: 180px; }
.hist-item { flex: 1; display: flex; flex-direction: column; align-items: center; height: 100%; justify-content: flex-end; }
.hist-count { font-size: 11px; color: #606266; }
.hist-bar { width: 70%; border-radius: 2px 2px 0 0; min-height: 1px; }
.hist-label { font-size: 10px; color: #909399; margin-top: 4px; white-space: nowrap; }
</style>
