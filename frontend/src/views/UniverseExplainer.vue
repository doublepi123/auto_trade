<template>
  <div class="universe-explainer-page" data-testid="universe-explainer-page">
    <div class="page-header">
      <h3>Universe 选择解释器</h3>
      <p>解释标的入选 / 落选原因、得分拆解与同业对比</p>
    </div>

    <el-tabs v-model="activeTab" class="explainer-tabs">
      <el-tab-pane label="标的解释" name="symbol">
        <div class="symbol-query-row">
          <el-input
            v-model="symbolInput"
            placeholder="输入标的代码，如 AAPL.US"
            clearable
            data-testid="explainer-symbol-input"
            style="width: 260px"
            @keyup.enter="querySymbol"
          />
          <el-button type="primary" :loading="symbolLoading" data-testid="explainer-symbol-query" @click="querySymbol">查询</el-button>
        </div>

        <div v-loading="symbolLoading">
          <template v-if="symbolExplanation">
            <el-descriptions :column="3" border size="small" class="block">
              <el-descriptions-item label="是否入选">
                <el-tag :type="symbolExplanation.selected ? 'success' : 'danger'" data-testid="explainer-selected">
                  {{ symbolExplanation.selected ? '入选' : '落选' }}
                </el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="排名">
                {{ symbolExplanation.rank !== null ? symbolExplanation.rank : '—' }}
              </el-descriptions-item>
              <el-descriptions-item label="得分">
                {{ totalScore.toFixed(3) }}
              </el-descriptions-item>
            </el-descriptions>

            <div class="block">
              <div class="block-title">得分拆解</div>
              <div v-if="breakdownEntries.length" class="breakdown-list">
                <div v-for="item in breakdownEntries" :key="item.factor" class="breakdown-row">
                  <span class="breakdown-label">{{ item.factor }}</span>
                  <div class="breakdown-bar-track">
                    <div
                      class="breakdown-bar-fill"
                      :style="{ width: `${item.widthPct}%`, background: item.value >= 0 ? '#409eff' : '#f56c6c' }"
                    />
                  </div>
                  <span class="breakdown-value">{{ item.value.toFixed(3) }}</span>
                </div>
              </div>
              <el-empty v-else description="无得分数据" :image-size="40" />
            </div>

            <div class="block">
              <div class="block-title">硬过滤检查</div>
              <div class="filter-tags">
                <el-tag v-for="f in symbolExplanation.hard_filters_passed" :key="`p-${f}`" type="success" size="small">{{ f }}</el-tag>
                <el-tag v-for="f in symbolExplanation.hard_filters_failed" :key="`f-${f}`" type="danger" size="small">{{ f }}</el-tag>
                <span v-if="!symbolExplanation.hard_filters_passed.length && !symbolExplanation.hard_filters_failed.length" class="hint">—</span>
              </div>
            </div>

            <div class="block">
              <div class="block-title">同业对比 (Top 5)</div>
              <el-table :data="peerTop" size="small">
                <el-table-column prop="symbol" label="标的" min-width="110" />
                <el-table-column label="得分" min-width="120">
                  <template #default="{ row }">{{ row.score.toFixed(3) }}</template>
                </el-table-column>
                <el-table-column label="入选" min-width="80">
                  <template #default="{ row }">
                    <el-tag :type="row.selected ? 'success' : 'info'" size="small">{{ row.selected ? '是' : '否' }}</el-tag>
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </template>
          <el-empty v-else-if="!symbolLoading" description="输入标的代码查询解释" />
        </div>
      </el-tab-pane>

      <el-tab-pane label="运行概览" name="run">
        <div class="symbol-query-row">
          <el-input-number v-model="runIdInput" :min="0" placeholder="运行 ID（留空取最新）" data-testid="explainer-run-id" />
          <el-button type="primary" :loading="runLoading" data-testid="explainer-run-query" @click="queryRun">加载</el-button>
        </div>

        <div v-loading="runLoading">
          <template v-if="runExplanation">
            <el-descriptions :column="3" border size="small" class="block">
              <el-descriptions-item label="运行 ID">{{ runExplanation.run_id }}</el-descriptions-item>
              <el-descriptions-item label="日期">{{ runExplanation.as_of_date }}</el-descriptions-item>
              <el-descriptions-item label="状态">
                <el-tag size="small">{{ runExplanation.status }}</el-tag>
              </el-descriptions-item>
              <el-descriptions-item label="候选数">{{ runExplanation.total_candidates }}</el-descriptions-item>
              <el-descriptions-item label="入选数">{{ runExplanation.selected_count }}</el-descriptions-item>
              <el-descriptions-item label="覆盖率">{{ (runExplanation.coverage_ratio * 100).toFixed(1) }}%</el-descriptions-item>
            </el-descriptions>

            <div class="block">
              <div class="block-title">入选 Top</div>
              <el-table :data="runExplanation.top_selected" size="small">
                <el-table-column prop="symbol" label="标的" min-width="110" />
                <el-table-column label="得分" min-width="100">
                  <template #default="{ row }">{{ row.score.toFixed(3) }}</template>
                </el-table-column>
                <el-table-column prop="reason" label="原因" min-width="200" show-overflow-tooltip />
              </el-table>
            </div>

            <div class="block">
              <div class="block-title">落选 Top</div>
              <el-table :data="runExplanation.top_rejected" size="small">
                <el-table-column prop="symbol" label="标的" min-width="110" />
                <el-table-column label="得分" min-width="100">
                  <template #default="{ row }">{{ row.score.toFixed(3) }}</template>
                </el-table-column>
                <el-table-column prop="reason" label="原因" min-width="200" show-overflow-tooltip />
              </el-table>
            </div>
          </template>
          <el-empty v-else-if="!runLoading" description="加载最近一次运行以查看概览" />
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { explainRun, explainSymbol } from '../api/universeExplainer'
import type { RunExplanation, SymbolExplanation } from '../api/universeExplainer'
import { resolveErrorMessage } from '../utils/error'

interface BreakdownRow {
  factor: string
  value: number
  widthPct: number
}

const activeTab = ref<'symbol' | 'run'>('symbol')

// --- Symbol tab ---
const symbolInput = ref('')
const symbolLoading = ref(false)
const symbolExplanation = ref<SymbolExplanation | null>(null)

const breakdownEntries = computed<BreakdownRow[]>(() => {
  const data = symbolExplanation.value
  if (!data) return []
  const entries = Object.entries(data.score_breakdown)
  const maxAbs = entries.reduce((m, [, v]) => Math.max(m, Math.abs(v)), 0) || 1
  return entries
    .map(([factor, value]) => ({ factor, value, widthPct: Math.round((Math.abs(value) / maxAbs) * 100) }))
    .sort((a, b) => Math.abs(b.value) - Math.abs(a.value))
})

const totalScore = computed(() =>
  Object.values(symbolExplanation.value?.score_breakdown ?? {}).reduce((sum, v) => sum + v, 0),
)

const peerTop = computed(() => symbolExplanation.value?.peer_comparison.slice(0, 5) ?? [])

async function querySymbol() {
  const sym = symbolInput.value.trim().toUpperCase()
  if (!sym) {
    ElMessage.warning('请输入标的代码')
    return
  }
  symbolLoading.value = true
  try {
    symbolExplanation.value = await explainSymbol(sym)
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '查询标的信息失败'))
  } finally {
    symbolLoading.value = false
  }
}

// --- Run tab ---
const runIdInput = ref<number | undefined>(undefined)
const runLoading = ref(false)
const runExplanation = ref<RunExplanation | null>(null)

async function queryRun() {
  runLoading.value = true
  try {
    runExplanation.value = await explainRun(runIdInput.value || undefined)
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '加载运行概览失败'))
  } finally {
    runLoading.value = false
  }
}

onMounted(queryRun)
</script>

<style scoped>
.universe-explainer-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px;
  background: #fff;
  min-height: calc(100vh - 120px);
}

.page-header h3 {
  margin: 0;
}

.page-header p {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
}

.symbol-query-row {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
}

.block {
  margin-top: 16px;
}

.block-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
  color: #303133;
}

.breakdown-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-width: 640px;
}

.breakdown-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
}

.breakdown-label {
  width: 160px;
  color: #606266;
  flex-shrink: 0;
}

.breakdown-bar-track {
  flex: 1;
  height: 10px;
  background: #f0f2f5;
  border-radius: 5px;
  overflow: hidden;
}

.breakdown-bar-fill {
  height: 100%;
  border-radius: 5px;
  transition: width 0.2s;
}

.breakdown-value {
  width: 60px;
  text-align: right;
  color: #303133;
  flex-shrink: 0;
}

.filter-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.hint {
  color: #909399;
  font-size: 13px;
}
</style>
