<template>
  <div class="risk-timeline-page" data-testid="risk-timeline-page">
    <div class="page-header">
      <div>
        <h3>风控检查时间线</h3>
        <p>按时间倒序展示风控检查步骤、通过率与阻断原因</p>
      </div>
      <div class="page-actions">
        <el-input
          v-model="symbolFilter"
          placeholder="标的过滤"
          clearable
          data-testid="risk-symbol-filter"
          style="width: 160px"
          @keyup.enter="reload"
          @clear="reload"
        />
        <el-select v-model="hoursFilter" style="width: 120px" data-testid="risk-hours-filter" @change="reload">
          <el-option label="近 1 小时" :value="1" />
          <el-option label="近 6 小时" :value="6" />
          <el-option label="近 24 小时" :value="24" />
          <el-option label="近 72 小时" :value="72" />
        </el-select>
        <el-button type="primary" :loading="loading" data-testid="risk-refresh" @click="reload">刷新</el-button>
      </div>
    </div>

    <el-row :gutter="12" v-loading="loading">
      <el-col :span="8">
        <el-card shadow="hover">
          <el-statistic title="总检查" :value="summary?.total_checks ?? 0" />
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover">
          <el-statistic title="通过" :value="summary?.passed ?? 0" />
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="hover">
          <el-statistic title="阻止" :value="summary?.blocked ?? 0" />
        </el-card>
      </el-col>
    </el-row>

    <div v-if="categoryEntries.length" class="category-block" data-testid="risk-category-breakdown">
      <span class="block-title">分类汇总</span>
      <div class="category-tags">
        <el-tag v-for="c in categoryEntries" :key="c.category" size="small" class="category-tag">
          {{ c.category }}：通过 {{ c.passed }} / 阻止 {{ c.blocked }}
        </el-tag>
      </div>
    </div>

    <el-empty v-if="!loading && checks.length === 0" description="暂无风控检查记录" />

    <el-timeline v-else class="timeline" data-testid="risk-timeline">
      <el-timeline-item
        v-for="(step, idx) in checks"
        :key="idx"
        :timestamp="formatDateTime(step.timestamp)"
        placement="top"
        :color="step.passed ? '#67c23a' : '#f56c6c'"
      >
        <el-card shadow="never" class="step-card">
          <div class="step-head">
            <span class="step-symbol">{{ step.symbol || '—' }}</span>
            <span class="step-check">{{ step.check_name }}</span>
            <el-tag size="small" :type="step.passed ? 'success' : 'danger'" data-testid="risk-step-tag">
              {{ step.passed ? '通过' : '阻止' }}
            </el-tag>
            <el-tag size="small" type="info" effect="plain">{{ step.category }}</el-tag>
          </div>
          <p class="step-reason">{{ step.reason }}</p>
        </el-card>
      </el-timeline-item>
    </el-timeline>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getRiskChecks, getRiskSummary } from '../api/riskTimeline'
import type { RiskCheckStep, RiskSummary } from '../api/riskTimeline'
import { resolveErrorMessage } from '../utils/error'

interface CategoryEntry {
  category: string
  passed: number
  blocked: number
}

const checks = ref<RiskCheckStep[]>([])
const summary = ref<RiskSummary | null>(null)
const loading = ref(false)
const symbolFilter = ref('')
const hoursFilter = ref(24)

const categoryEntries = computed<CategoryEntry[]>(() => {
  const data = summary.value?.by_category
  if (!data) return []
  return Object.entries(data).map(([category, v]) => ({
    category,
    passed: v.passed,
    blocked: v.blocked,
  }))
})

function formatDateTime(value: string): string {
  return new Date(value).toLocaleString([], {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

async function reload() {
  loading.value = true
  const symbol = symbolFilter.value.trim().toUpperCase() || undefined
  try {
    const [checkList, sum] = await Promise.all([
      getRiskChecks({ symbol, limit: 100 }),
      getRiskSummary(hoursFilter.value),
    ])
    checks.value = checkList
    summary.value = sum
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '加载风控时间线失败'))
  } finally {
    loading.value = false
  }
}

onMounted(reload)
</script>

<style scoped>
.risk-timeline-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px;
  background: #fff;
  min-height: calc(100vh - 120px);
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.page-header h3 {
  margin: 0;
}

.page-header p {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
}

.page-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

.block-title {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
  margin-right: 8px;
}

.category-block {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
}

.category-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.timeline {
  padding-left: 8px;
}

.step-card {
  margin-bottom: 0;
}

.step-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.step-symbol {
  font-weight: 600;
  color: #303133;
}

.step-check {
  color: #606266;
  font-size: 13px;
}

.step-reason {
  margin: 6px 0 0;
  color: #606266;
  font-size: 13px;
  line-height: 1.5;
}

@media (max-width: 720px) {
  .page-header {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
