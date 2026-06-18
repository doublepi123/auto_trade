<template>
  <div class="lab-page" data-testid="lab-page">
    <h2>LLM 优化工作台</h2>
    <el-tabs v-model="activeTab" data-testid="lab-tabs">
      <el-tab-pane label="实验与版本" name="experiments">
        <div data-testid="tab-experiments">
          <el-card header="Prompt 版本">
            <el-table :data="versions" data-testid="versions-table">
              <el-table-column prop="name" label="名称" />
              <el-table-column prop="version" label="版本" />
              <el-table-column prop="description" label="说明" />
              <el-table-column label="激活">
                <template #default="{ row }">
                  <el-tag v-if="row.is_active" type="success">激活中</el-tag>
                  <el-button v-else size="small" @click="activate(row)" data-testid="activate-btn">设为激活</el-button>
                </template>
              </el-table-column>
              <el-table-column prop="created_at" label="创建时间" />
            </el-table>
          </el-card>

          <el-card header="新建版本" style="margin-top: 12px">
            <el-form label-width="90px">
              <el-form-item label="名称"><el-input v-model="newVersion.name" data-testid="v-name" /></el-form-item>
              <el-form-item label="版本号"><el-input v-model="newVersion.version" data-testid="v-version" /></el-form-item>
              <el-form-item label="说明"><el-input v-model="newVersion.description" /></el-form-item>
              <el-form-item label="模板">
                <el-input v-model="newVersion.template" type="textarea" :rows="6" data-testid="v-template" />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" :loading="creating" @click="submitVersion" data-testid="create-version-btn">创建</el-button>
              </el-form-item>
            </el-form>
          </el-card>

          <el-card header="实验摘要" style="margin-top: 12px">
            <el-select v-model="selectedSummaryExp" placeholder="选择实验" @change="loadSummary" data-testid="summary-exp-select">
              <el-option v-for="n in experimentNames" :key="n" :label="n" :value="n" />
            </el-select>
            <el-table :data="summary" style="margin-top: 8px">
              <el-table-column prop="variant_name" label="变体" />
              <el-table-column prop="total_count" label="样本" />
              <el-table-column prop="profitable_count" label="盈利数" />
              <el-table-column label="胜率"><template #default="{ row }">{{ pct(row.win_rate) }}</template></el-table-column>
              <el-table-column prop="avg_pnl" label="平均PnL" />
            </el-table>
          </el-card>
        </div>
      </el-tab-pane>

      <el-tab-pane label="性能看板" name="performance">
        <div data-testid="tab-performance">
          <el-select v-model="perfExp" placeholder="选择实验" @change="loadPerformance" data-testid="perf-exp-select">
            <el-option v-for="n in experimentNames" :key="n" :label="n" :value="n" />
          </el-select>
          <el-empty v-if="!perfExp" description="请选择实验" />
          <template v-else>
            <el-row :gutter="12" style="margin-top: 12px" data-testid="perf-stats">
              <el-col :span="6"><el-statistic title="总交易" :value="Number(stats?.total_trades ?? 0)" /></el-col>
              <el-col :span="6"><el-statistic title="胜率" :value="Number((stats?.win_rate ?? 0) * 100)" suffix="%" /></el-col>
              <el-col :span="6"><el-statistic title="总PnL" :value="Number(stats?.total_pnl ?? 0)" /></el-col>
              <el-col :span="6"><el-statistic title="平均PnL" :value="Number(stats?.avg_pnl ?? 0)" /></el-col>
            </el-row>
            <el-table :data="variants" style="margin-top: 12px" data-testid="perf-variants">
              <el-table-column prop="variant" label="变体" />
              <el-table-column prop="total_trades" label="交易数" />
              <el-table-column label="胜率"><template #default="{ row }">{{ pct(row.win_rate) }}</template></el-table-column>
              <el-table-column prop="total_pnl" label="总PnL" />
              <el-table-column prop="avg_pnl" label="平均PnL" />
            </el-table>
            <el-card header="优化建议" style="margin-top: 12px" data-testid="perf-recommendations">
              <ul><li v-for="(r, i) in recommendations" :key="i">{{ r }}</li></ul>
            </el-card>
          </template>
        </div>
      </el-tab-pane>

      <el-tab-pane label="指标面板" name="indicators">
        <div data-testid="tab-indicators">
          <el-input v-model="indicatorSymbol" placeholder="标的（留空取当前策略）" style="width: 240px" data-testid="indicator-symbol" />
          <el-button type="primary" :loading="indicatorsLoading" @click="loadIndicators" data-testid="load-indicators-btn">查询</el-button>
          <span style="margin-left: 8px; color: #909399">实时快照，非历史复盘</span>

          <el-empty v-if="indicators && !indicators.available" description="行情不可用（broker 凭证缺失或限流）" data-testid="indicators-unavailable" />
          <el-row v-else-if="indicators && indicators.available" :gutter="12" style="margin-top: 12px" data-testid="indicators-grid">
            <el-col :span="8"><el-card header="RSI(14)">{{ indicators.rsi?.toFixed(2) }}</el-card></el-col>
            <el-col :span="8"><el-card header="MACD">macd {{ indicators.macd?.macd?.toFixed(3) }} / signal {{ indicators.macd?.signal?.toFixed(3) }} / hist {{ indicators.macd?.histogram?.toFixed(3) }}</el-card></el-col>
            <el-col :span="8"><el-card header="成交量">量比 {{ indicators.volume_analysis?.volume_ratio?.toFixed(2) }}（{{ indicators.volume_analysis?.trend }}）</el-card></el-col>
            <el-col :span="8" style="margin-top: 12px"><el-card header="市场情绪">{{ indicators.sentiment?.sentiment }}（{{ indicators.sentiment?.score?.toFixed(2) }}）<br>{{ indicators.sentiment?.description }}</el-card></el-col>
            <el-col :span="8" style="margin-top: 12px"><el-card header="多时间框架">{{ indicators.multi_timeframe?.description }}<br>对齐：{{ indicators.multi_timeframe?.aligned ? '是' : '否' }}</el-card></el-col>
            <el-col :span="8" style="margin-top: 12px"><el-card header="ATR / 布林带">ATR {{ indicators.atr?.toFixed(3) }}<br>上 {{ indicators.bb_upper?.toFixed(2) }} / 中 {{ indicators.bb_middle?.toFixed(2) }} / 下 {{ indicators.bb_lower?.toFixed(2) }}</el-card></el-col>
          </el-row>
        </div>
      </el-tab-pane>

      <el-tab-pane label="运行状态" name="runtime">
        <div data-testid="tab-runtime" v-loading="runtimeLoading">
          <div class="runtime-toolbar">
            <span class="muted">只读监控：LLM 调度状态、预算、symbol 状态与最近交互</span>
            <el-button type="primary" size="small" :loading="runtimeLoading" @click="loadRuntimeStatus">刷新</el-button>
          </div>

          <el-row v-if="runtimeStatus" :gutter="12" class="runtime-grid" data-testid="llm-runtime-overview">
            <el-col :xs="24" :sm="8">
              <el-card header="总览">
                <p><el-tag :type="runtimeStatus.enabled ? 'success' : 'info'">{{ runtimeStatus.enabled ? '已启用' : '未启用' }}</el-tag></p>
                <p>间隔：{{ runtimeStatus.interval_minutes }} 分钟</p>
                <p>最近分析：{{ formatDateTime(runtimeStatus.last_analysis_at) }}</p>
                <p>下次分析：{{ formatDateTime(runtimeStatus.next_analysis_at) }}</p>
              </el-card>
            </el-col>
            <el-col :xs="24" :sm="8">
              <el-card header="当前建议">
                <template v-if="runtimeStatus.current_suggestion">
                  <p>买入下沿 {{ runtimeStatus.current_suggestion.buy_low.toFixed(2) }}</p>
                  <p>卖出上沿 {{ runtimeStatus.current_suggestion.sell_high.toFixed(2) }}</p>
                  <p>置信度 {{ pct(runtimeStatus.current_suggestion.confidence_score) }}</p>
                  <p v-if="runtimeStatus.current_suggestion.analysis" class="runtime-analysis">{{ runtimeStatus.current_suggestion.analysis }}</p>
                </template>
                <el-empty v-else description="暂无建议" />
              </el-card>
            </el-col>
            <el-col :xs="24" :sm="8">
              <el-card header="应用值">
                <template v-if="runtimeStatus.applied_values">
                  <p>buy_low {{ runtimeStatus.applied_values.buy_low.toFixed(2) }}</p>
                  <p>sell_high {{ runtimeStatus.applied_values.sell_high.toFixed(2) }}</p>
                </template>
                <p v-if="runtimeStatus.reject_reason" class="negative">拒绝：{{ runtimeStatus.reject_reason }}</p>
                <el-empty v-if="!runtimeStatus.applied_values && !runtimeStatus.reject_reason" description="暂无应用记录" />
              </el-card>
            </el-col>
          </el-row>

          <el-card v-if="runtimeStatus" header="预算" class="runtime-card" data-testid="llm-runtime-budget">
            <el-row :gutter="12">
              <el-col :xs="12" :sm="4"><el-statistic title="每轮 symbol" :value="runtimeStatus.budget.max_symbols_per_cycle" /></el-col>
              <el-col :xs="12" :sm="4"><el-statistic title="每小时上限" :value="runtimeStatus.budget.max_analyses_per_hour" /></el-col>
              <el-col :xs="12" :sm="4"><el-statistic title="跟踪标的" :value="runtimeStatus.budget.tracked_symbol_count" /></el-col>
              <el-col :xs="12" :sm="4"><el-statistic title="有效预算" :value="runtimeStatus.budget.effective_symbol_budget" /></el-col>
              <el-col :xs="12" :sm="4"><el-statistic title="已用" :value="runtimeStatus.budget.used_analyses_last_hour" /></el-col>
              <el-col :xs="12" :sm="4"><el-statistic title="剩余" :value="runtimeStatus.budget.remaining_analyses_this_hour" /></el-col>
            </el-row>
          </el-card>

          <el-card v-if="runtimeHealthHints.length" header="健康提示" class="runtime-card" data-testid="llm-runtime-health">
            <el-alert
              v-for="hint in runtimeHealthHints"
              :key="hint"
              :title="hint"
              type="warning"
              show-icon
              :closable="false"
              class="runtime-alert"
            />
          </el-card>

          <el-card v-if="runtimeStatus" header="Symbol 状态" class="runtime-card" data-testid="llm-runtime-symbols">
            <el-table :data="runtimeStatus.symbol_statuses" size="small">
              <el-table-column prop="symbol" label="标的" min-width="110">
                <template #default="{ row }">
                  <strong>{{ row.symbol }}</strong>
                  <el-tag v-if="row.is_primary" size="small" type="primary" style="margin-left: 4px">主</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="market" label="市场" width="80" />
              <el-table-column label="挂单" width="80">
                <template #default="{ row }">{{ row.has_pending_order ? '有' : '无' }}</template>
              </el-table-column>
              <el-table-column label="最近分析" min-width="170">
                <template #default="{ row }">{{ formatDateTime(row.last_analysis_at) }}</template>
              </el-table-column>
              <el-table-column label="下次分析" min-width="170">
                <template #default="{ row }">{{ formatDateTime(row.next_analysis_at) }}</template>
              </el-table-column>
              <el-table-column prop="last_status" label="状态" min-width="100" />
              <el-table-column prop="last_skip_reason" label="跳过原因" min-width="180">
                <template #default="{ row }">{{ row.last_skip_reason || '-' }}</template>
              </el-table-column>
            </el-table>
          </el-card>

          <el-card header="最近交互" class="runtime-card" data-testid="llm-runtime-interactions">
            <el-table :data="runtimeInteractions" size="small">
              <el-table-column prop="id" label="ID" width="70" />
              <el-table-column prop="interaction_type" label="类型" width="100" />
              <el-table-column prop="symbol" label="标的" width="110" />
              <el-table-column label="结果" width="100">
                <template #default="{ row }"><el-tag :type="row.success ? 'success' : 'danger'">{{ row.success ? '成功' : '失败' }}</el-tag></template>
              </el-table-column>
              <el-table-column label="应用" width="90">
                <template #default="{ row }"><el-tag :type="row.applied ? 'primary' : 'info'">{{ row.applied ? '已应用' : '未应用' }}</el-tag></template>
              </el-table-column>
              <el-table-column prop="order_action" label="动作" width="100" />
              <el-table-column prop="error" label="错误" min-width="160">
                <template #default="{ row }">{{ row.error || '-' }}</template>
              </el-table-column>
              <el-table-column label="时间" min-width="170">
                <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
              </el-table-column>
            </el-table>
          </el-card>
        </div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, reactive, onMounted, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listPromptVersions, createPromptVersion, activatePromptVersion,
  listExperimentNames, getExperimentSummary,
  getPerformanceStats, comparePerformanceVariants, getPerformanceRecommendations,
  getIndicators,
} from '../api/lab'
import { getLLMInteractions, getLLMIntervalStatus } from '../api/llm_advisor'
import type {
  PromptVersion, ExperimentSummary, PerformanceStats,
  PerformanceVariant, IndicatorsResponse, LLMInteractionRecord, LLMIntervalStatus,
} from '../types'
import { resolveErrorMessage } from '../utils/error'

const activeTab = ref('experiments')

// --- Tab 1: versions & experiments ---
const versions = ref<PromptVersion[]>([])
const newVersion = reactive({ name: '', version: '', description: '', template: '' })
const creating = ref(false)
const experimentNames = ref<string[]>([])
const selectedSummaryExp = ref('')
const summary = ref<ExperimentSummary[]>([])

async function loadVersions() {
  try {
    versions.value = await listPromptVersions()
  } catch {
    ElMessage.error('加载版本失败')
  }
}
async function loadExperimentNames() {
  try {
    experimentNames.value = await listExperimentNames()
  } catch {
    ElMessage.error('加载实验列表失败')
  }
}
async function submitVersion() {
  if (!newVersion.name || !newVersion.version || !newVersion.template) {
    ElMessage.warning('name / version / template 必填')
    return
  }
  creating.value = true
  try {
    await createPromptVersion({ ...newVersion })
    ElMessage.success('版本已创建')
    newVersion.name = ''; newVersion.version = ''; newVersion.description = ''; newVersion.template = ''
    await loadVersions()
  } catch (e: unknown) {
    ElMessage.error(resolveErrorMessage(e, '创建失败'))
  } finally {
    creating.value = false
  }
}
async function activate(v: PromptVersion) {
  try {
    await ElMessageBox.confirm(`确认将 "${v.name} ${v.version}" 设为激活版本？`, '确认激活')
  } catch {
    return
  }
  try {
    await activatePromptVersion(v.id)
    ElMessage.success('已激活')
    await loadVersions()
  } catch {
    ElMessage.error('激活失败')
  }
}
async function loadSummary() {
  if (!selectedSummaryExp.value) return
  try {
    summary.value = await getExperimentSummary(selectedSummaryExp.value)
  } catch {
    ElMessage.error('加载实验摘要失败')
  }
}

// --- Tab 2: performance ---
const perfExp = ref('')
const stats = ref<PerformanceStats | null>(null)
const variants = ref<PerformanceVariant[]>([])
const recommendations = ref<string[]>([])

async function loadPerformance() {
  if (!perfExp.value) { stats.value = null; variants.value = []; recommendations.value = []; return }
  try {
    const [s, c, r] = await Promise.all([
      getPerformanceStats(perfExp.value),
      comparePerformanceVariants(perfExp.value),
      getPerformanceRecommendations(perfExp.value),
    ])
    stats.value = s; variants.value = c; recommendations.value = r
  } catch {
    ElMessage.error('加载性能数据失败')
  }
}

// --- Tab 3: indicators ---
const indicatorSymbol = ref('')
const indicators = ref<IndicatorsResponse | null>(null)
const indicatorsLoading = ref(false)

async function loadIndicators() {
  indicatorsLoading.value = true
  try {
    indicators.value = await getIndicators(indicatorSymbol.value || undefined)
    indicatorSymbol.value = indicators.value.symbol
  } catch (e: unknown) {
    ElMessage.error(resolveErrorMessage(e, '指标加载失败'))
  } finally {
    indicatorsLoading.value = false
  }
}

function pct(v: number): string { return `${(v * 100).toFixed(1)}%` }

// --- Tab 4: LLM runtime observability ---
const runtimeStatus = ref<LLMIntervalStatus | null>(null)
const runtimeInteractions = ref<LLMInteractionRecord[]>([])
const runtimeLoading = ref(false)
const runtimeLoaded = ref(false)

const runtimeHealthHints = computed(() => {
  const status = runtimeStatus.value
  if (!status) return []
  const hints: string[] = []
  if (!status.enabled) hints.push('LLM 区间分析未启用')
  if (status.budget.remaining_analyses_this_hour <= 0) hints.push('预算已耗尽')
  if (!status.next_analysis_at) hints.push('暂无下一次分析时间')
  for (const row of status.symbol_statuses) {
    if (row.last_skip_reason) hints.push(`${row.symbol}: ${row.last_skip_reason}`)
    if (row.has_pending_order) hints.push(`${row.symbol}: 存在挂单，可能跳过分析或交易动作`)
  }
  return hints
})

async function loadRuntimeStatus() {
  runtimeLoading.value = true
  try {
    const [status, interactions] = await Promise.all([
      getLLMIntervalStatus(),
      getLLMInteractions(20),
    ])
    runtimeStatus.value = status
    runtimeInteractions.value = interactions
    runtimeLoaded.value = true
  } catch (e: unknown) {
    ElMessage.error(resolveErrorMessage(e, '加载 LLM 运行状态失败'))
  } finally {
    runtimeLoading.value = false
  }
}

function formatDateTime(value: string | null): string {
  if (!value) return '-'
  return new Date(value).toLocaleString([], {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  })
}

watch(activeTab, (tab) => {
  if (tab === 'runtime' && !runtimeLoaded.value && !runtimeLoading.value) void loadRuntimeStatus()
})

onMounted(async () => {
  await Promise.all([loadVersions(), loadExperimentNames()])
})
</script>

<style scoped>
.lab-page {
  padding: 16px;
}

.runtime-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.muted {
  color: #6b7280;
  font-size: 13px;
}

.runtime-grid {
  margin-bottom: 12px;
}

.runtime-card {
  margin-top: 12px;
}

.runtime-alert + .runtime-alert {
  margin-top: 8px;
}

.negative {
  color: #c43838;
}

.runtime-analysis {
  color: #4b5563;
  line-height: 1.5;
}
</style>
