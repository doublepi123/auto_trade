<script setup lang="ts">
import { computed, ref } from 'vue'
import { getSkipAnalyticsSummary, type SkipAnalyticsResult } from '../api/skipAnalytics'
import { skipCategoryLabel } from '../utils/labels'

const loading = ref(false)
const result = ref<SkipAnalyticsResult | null>(null)
const days = ref(30)

async function run() {
  loading.value = true
  try {
    result.value = await getSkipAnalyticsSummary({ days: days.value })
  } finally {
    loading.value = false
  }
}

const categoryColor: Record<string, string> = {
  FEE: '#e6a23c',
  RISK: '#f56c6c',
  PENDING: '#409eff',
  POSITION: '#909399',
  SESSION: '#b88230',
  COOLDOWN: '#a0cfff',
  REPRICING: '#c45656',
  UNKNOWN: '#dcdfe6',
}

const maxDaily = computed(() => {
  if (!result.value?.daily?.length) return 0
  return Math.max(...result.value.daily.map((d) => d.count))
})

const label = skipCategoryLabel
</script>

<template>
  <div class="page-container">
    <h2>跳过原因分析</h2>
    <p class="page-desc">ORDER_SKIPPED 事件按跳过类别聚合：为何不下单、分标的与每日趋势（灵感来自 Freqtrade 信号拒绝分析）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="回看天数">
          <el-input-number v-model="days" :min="1" :max="3650" :step="7" />
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
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="跳过事件总数" :value="result.sample_size" />
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="涉及标的" :value="result.by_symbol.length" />
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">最多类别</span>
                <span class="stat-value" v-if="result.by_category.length">
                  {{ label(result.by_category[0].category) }}
                </span>
                <span class="stat-sub" v-if="result.by_category.length">
                  {{ (result.by_category[0].share * 100).toFixed(1) }}%
                </span>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>类别分布</template>
          <div v-for="row in result.by_category" :key="row.category" class="cat-row">
            <span class="cat-label">{{ label(row.category) }}</span>
            <div class="cat-bar-wrap">
              <div
                class="cat-bar"
                :style="{
                  width: (row.share * 100).toFixed(1) + '%',
                  background: categoryColor[row.category] || '#409eff',
                }"
              />
            </div>
            <span class="cat-count">{{ row.count }} ({{ (row.share * 100).toFixed(1) }}%)</span>
          </div>
        </el-card>

        <el-card style="margin-top: 16px" v-if="result.daily.length">
          <template #header>每日跳过数</template>
          <div class="bar-chart">
            <div v-for="d in result.daily" :key="d.date" class="bar-item" :title="`${d.date}: ${d.count}`">
              <div class="bar" :style="{ height: maxDaily > 0 ? (d.count / maxDaily) * 100 + '%' : '0%' }" />
              <span class="bar-label">{{ d.date.slice(5) }}</span>
            </div>
          </div>
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>分标的</template>
          <el-table :data="result.by_symbol" size="small" max-height="400">
            <el-table-column prop="symbol" label="标的" width="120" />
            <el-table-column prop="total" label="总数" width="80" />
            <el-table-column label="类别明细">
              <template #default="{ row }">
                <el-tag
                  v-for="(n, cat) in row.by_category"
                  :key="cat"
                  size="small"
                  style="margin-right: 6px"
                >
                  {{ label(String(cat)) }}: {{ n }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>高频原因</template>
          <div v-for="group in result.top_reasons" :key="group.category" style="margin-bottom: 12px">
            <div class="reason-cat">{{ label(group.category) }}</div>
            <div v-for="r in group.reasons" :key="r.message" class="reason-line">
              <el-tag size="small" effect="plain">{{ r.count }}×</el-tag>
              <span class="reason-msg">{{ r.message }}</span>
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
.cat-row { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
.cat-label { width: 90px; font-size: 13px; }
.cat-bar-wrap { flex: 1; background: #f5f7fa; border-radius: 4px; height: 16px; }
.cat-bar { height: 100%; border-radius: 4px; min-width: 2px; }
.cat-count { width: 110px; font-size: 12px; color: #606266; text-align: right; }
.bar-chart { display: flex; align-items: flex-end; gap: 2px; height: 140px; overflow-x: auto; }
.bar-item { display: flex; flex-direction: column; align-items: center; min-width: 18px; height: 100%; justify-content: flex-end; }
.bar { width: 12px; background: #409eff; border-radius: 2px 2px 0 0; }
.bar-label { font-size: 9px; color: #909399; transform: rotate(-45deg); white-space: nowrap; margin-top: 4px; }
.reason-cat { font-weight: 600; margin-bottom: 4px; }
.reason-line { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
.reason-msg { font-size: 12px; color: #606266; }
</style>
