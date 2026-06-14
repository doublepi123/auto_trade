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
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listPromptVersions, createPromptVersion, activatePromptVersion,
  listExperimentNames, getExperimentSummary,
  getPerformanceStats, comparePerformanceVariants, getPerformanceRecommendations,
  getIndicators,
} from '../api/lab'
import type {
  PromptVersion, ExperimentSummary, PerformanceStats,
  PerformanceVariant, IndicatorsResponse,
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

onMounted(async () => {
  await Promise.all([loadVersions(), loadExperimentNames()])
})
</script>

<style scoped>
.lab-page {
  padding: 16px;
}
</style>
