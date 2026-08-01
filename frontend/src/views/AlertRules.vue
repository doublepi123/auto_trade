<template>
  <div class="alerts-page">
    <div class="alerts-header">
      <h3>告警规则</h3>
      <div class="alerts-actions">
        <el-button :loading="loading" @click="loadRules">刷新</el-button>
        <el-button type="primary" plain :loading="evaluating" data-testid="alert-evaluate" @click="evaluate">立即评估</el-button>
        <el-button plain size="small" :disabled="rules.length === 0" data-testid="alert-export-json" @click="exportRulesJson">导出 JSON</el-button>
        <el-button plain size="small" data-testid="alert-import-json" @click="triggerImportRulesJson">导入 JSON</el-button>
        <input ref="rulesImportInput" type="file" accept=".json,application/json" style="display: none" data-testid="alert-import-input" @change="handleImportRulesJson" />
        <el-button type="primary" data-testid="alert-create" @click="openCreate">新建规则</el-button>
      </div>
    </div>

    <p v-if="evalResult" class="eval-note">
      已评估 {{ evalResult.evaluated }} 条，触发 {{ evalResult.fired }} 条（{{ evalResult.skipped_cooldown }} 条冷却中跳过）。
    </p>
    <p class="hint">
      后台每 60 秒评估一次启用规则：价格类用实时行情，日内亏损类读 <code>runtime_state.daily_pnl</code>；触发后按冷却时间节流，经已配置通知渠道推送。
    </p>

    <div class="summary-grid">
      <div class="summary-card" data-testid="alert-rule-health">
        <div class="summary-title">规则健康</div>
        <div class="summary-items">
          <span>总计 {{ rules.length }}</span>
          <span>启用 {{ enabledRules.length }}</span>
          <span>停用 {{ disabledRules.length }}</span>
          <span>有触发记录 {{ effectivenessLoaded ? firedRuleCount : '—' }}</span>
          <span>从未触发 {{ effectivenessLoaded ? neverFiredRuleCount : '—' }}</span>
        </div>
        <div class="summary-empty">触发统计来自服务端全量聚合（见下方明细），未加载时显示 —。</div>
      </div>
      <div class="summary-card" data-testid="alert-effectiveness-summary">
        <div class="summary-title">触发统计（服务端）</div>
        <div class="effectiveness-controls">
          <span data-testid="alert-effectiveness-range">
            <el-date-picker
              v-model="effectivenessRange"
              type="daterange"
              range-separator="至"
              start-placeholder="开始日期"
              end-placeholder="结束日期"
              value-format="YYYY-MM-DD"
              clearable
              size="small"
              style="width: 240px"
            />
          </span>
          <el-button size="small" :loading="effectivenessLoading" data-testid="alert-effectiveness-reload" @click="loadEffectiveness">查询</el-button>
        </div>
        <div v-if="effectivenessError" class="summary-error" data-testid="alert-effectiveness-error">
          {{ effectivenessError }}
          <el-button link size="small" type="primary" @click="loadEffectiveness">重试</el-button>
        </div>
        <template v-else-if="effectivenessLoaded">
          <div class="summary-items">
            <span>规则总数 {{ effectivenessTotal }}</span>
            <span>窗口内触发 {{ windowFiringTotal }} 次</span>
            <span>窗口内有触发 {{ windowFiredRuleCount }} 条</span>
            <span>从未触发（全量） {{ neverFiredRuleCount }} 条</span>
          </div>
          <div class="summary-empty" data-testid="alert-effectiveness-window">{{ effectivenessWindowLabel }}</div>
        </template>
        <div v-else class="summary-empty" data-testid="alert-effectiveness-placeholder">
          {{ effectivenessLoading ? '加载中…' : '尚未加载触发统计' }}
        </div>
      </div>
      <div class="summary-card" data-testid="alert-recent-firings">
        <div class="summary-title">上次触发规则（全量）</div>
        <div v-if="recentlyFiredRules.length" class="summary-items">
          <span v-for="rule in recentlyFiredRules" :key="rule.id">
            {{ rule.name }} · {{ formatDateTime(ruleLastFiredAt(rule) ?? '') }}
          </span>
        </div>
        <div v-else class="summary-empty">暂无触发记录</div>
      </div>
    </div>

    <div class="filter-row">
      <el-button size="small" :type="activeFilter === 'all' ? 'primary' : 'default'" data-testid="alert-filter-all" @click="activeFilter = 'all'">全部</el-button>
      <el-button size="small" :type="activeFilter === 'enabled' ? 'primary' : 'default'" data-testid="alert-filter-enabled" @click="activeFilter = 'enabled'">启用</el-button>
      <el-button size="small" :type="activeFilter === 'disabled' ? 'primary' : 'default'" data-testid="alert-filter-disabled" @click="activeFilter = 'disabled'">停用</el-button>
      <el-button size="small" :type="activeFilter === 'recent-fired' ? 'primary' : 'default'" data-testid="alert-filter-recent-fired" @click="activeFilter = 'recent-fired'">最近触发</el-button>
      <el-button size="small" :type="activeFilter === 'never-fired' ? 'primary' : 'default'" data-testid="alert-filter-never-fired" @click="activeFilter = 'never-fired'">从未触发</el-button>
    </div>

    <el-table :data="filteredRules" size="small" class="responsive-table" v-loading="loading">
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column label="标的" min-width="90">
        <template #default="{ row }">{{ ruleSymbolLabel(row) }}</template>
      </el-table-column>
      <el-table-column label="条件" min-width="130">
        <template #default="{ row }">{{ ruleConditionLabel(row) }}</template>
      </el-table-column>
      <el-table-column label="严重度" min-width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="severityType(row.severity)">{{ row.severity }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="启用" min-width="70">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled" @change="(v: boolean | string | number) => toggleEnabled(row, v)" />
        </template>
      </el-table-column>
      <el-table-column label="冷却(秒)" min-width="80">
        <template #default="{ row }">{{ row.cooldown_seconds }}</template>
      </el-table-column>
      <el-table-column label="上次触发" min-width="140">
        <template #default="{ row }">{{ row.last_fired_at ? formatDateTime(row.last_fired_at) : '—' }}</template>
      </el-table-column>
      <el-table-column label="" width="160">
        <template #default="{ row }">
          <el-button link size="small" data-testid="alert-history" @click="openHistory(row)">历史</el-button>
          <el-button link size="small" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" size="small" @click="remove(row.id)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="effectiveness-section" data-testid="alert-effectiveness">
      <div class="effectiveness-header">
        <h4>触发统计明细</h4>
        <span class="hint" data-testid="alert-effectiveness-note">
          覆盖全部规则；触发次数按所选窗口统计，「最近触发」与「从未触发」为全量口径。
        </span>
      </div>
      <div v-if="effectivenessError" class="effectiveness-state" data-testid="alert-effectiveness-detail-error">
        <el-alert type="error" :title="effectivenessError" :closable="false" show-icon />
        <el-button size="small" style="margin-top: 8px" data-testid="alert-effectiveness-retry" @click="loadEffectiveness">重新加载</el-button>
      </div>
      <div v-else-if="effectivenessLoading" class="effectiveness-state" data-testid="alert-effectiveness-loading">加载中…</div>
      <el-table v-else-if="effectivenessItems.length" :data="effectivenessItems" size="small" class="responsive-table" data-testid="alert-effectiveness-table">
        <el-table-column prop="name" label="名称" min-width="120" />
        <el-table-column label="标的" min-width="90">
          <template #default="{ row }">{{ row.symbol || '—' }}</template>
        </el-table-column>
        <el-table-column label="类型" min-width="120">
          <template #default="{ row }">{{ alertRuleTypeLabel(row.rule_type) }}</template>
        </el-table-column>
        <el-table-column label="窗口触发次数" min-width="100">
          <template #default="{ row }">{{ row.firing_count }}</template>
        </el-table-column>
        <el-table-column label="最近触发（全量）" min-width="140">
          <template #default="{ row }">{{ row.last_fired_at ? formatDateTime(row.last_fired_at) : '—' }}</template>
        </el-table-column>
        <el-table-column label="状态" min-width="100">
          <template #default="{ row }">
            <el-tag v-if="row.never_fired" size="small" type="info">从未触发</el-tag>
            <el-tag v-else size="small" type="success">有触发记录</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="启用" min-width="70">
          <template #default="{ row }">
            <el-tag size="small" :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag>
          </template>
        </el-table-column>
      </el-table>
      <div v-else class="effectiveness-state" data-testid="alert-effectiveness-empty">暂无规则触发统计数据</div>
    </div>

    <el-dialog v-model="dialog.visible" :title="dialog.id ? '编辑规则' : '新建规则'" width="480px" data-testid="alert-dialog">
      <el-form label-width="90px">
        <el-form-item label="名称">
          <el-input v-model="dialog.name" placeholder="规则名称" data-testid="alert-name" />
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="dialog.rule_type" data-testid="alert-rule-type" style="width: 100%" @change="handleRuleTypeChange">
            <el-option label="价格上穿 ≥" value="price_above" />
            <el-option label="价格下穿 ≤" value="price_below" />
            <el-option label="日内亏损 ≤" value="daily_loss" />
            <el-option label="连续亏损 ≥（账户级）" value="consecutive_losses" data-testid="alert-type-consecutive-losses" />
            <el-option label="熔断开关触发（账户级）" value="kill_switch_engaged" data-testid="alert-type-kill-switch" />
          </el-select>
        </el-form-item>
        <el-form-item label="标的">
          <el-input
            v-model="dialog.symbol"
            :placeholder="isAccountWideType ? '账户级规则，无需标的' : 'AAPL.US'"
            :disabled="isAccountWideType"
            data-testid="alert-symbol"
          />
          <div v-if="isAccountWideType" class="field-hint" data-testid="alert-account-wide-hint">
            账户级规则读取账户整体风控状态，标的固定留空。
          </div>
        </el-form-item>
        <el-form-item label="阈值">
          <template v-if="dialog.rule_type === 'kill_switch_engaged'">
            <el-input-number :model-value="1" disabled :precision="0" data-testid="alert-threshold-fixed" />
            <div class="field-hint">熔断开关启用时触发，阈值固定为 1。</div>
          </template>
          <template v-else-if="dialog.rule_type === 'consecutive_losses'">
            <el-input-number v-model="dialog.threshold" :precision="0" :step="1" data-testid="alert-threshold" />
            <div class="field-hint">连续亏损笔数达到阈值时触发，须为 ≥ 1 的整数。</div>
          </template>
          <template v-else>
            <el-input-number v-model="dialog.threshold" :precision="2" :step="1" data-testid="alert-threshold" />
            <div v-if="dialog.rule_type === 'daily_loss'" class="field-hint">日内盈亏（含符号）低于阈值时触发，例如 -500。</div>
          </template>
        </el-form-item>
        <el-form-item label="严重度">
          <el-select v-model="dialog.severity" style="width: 100%">
            <el-option label="INFO" value="INFO" />
            <el-option label="WARNING" value="WARNING" />
            <el-option label="CRITICAL" value="CRITICAL" />
          </el-select>
        </el-form-item>
        <el-form-item label="冷却(秒)">
          <el-input-number v-model="dialog.cooldown_seconds" :min="0" :max="86400" :step="60" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="dialog.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialog.visible = false">取消</el-button>
        <el-button type="primary" :loading="dialog.saving" data-testid="alert-save" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="historyDialog.visible"
      :title="`触发历史 · ${historyDialog.ruleName}`"
      width="640px"
      data-testid="alert-history-dialog"
    >
      <div v-loading="historyDialog.loading">
        <p class="eval-note">共 {{ historyDialog.items.length }} 次触发（最近优先）</p>

        <div class="history-chart" data-testid="alert-history-chart">
          <svg
            v-if="historySparkline.points.length > 1"
            viewBox="0 0 560 120"
            preserveAspectRatio="none"
            class="history-chart-svg"
          >
            <polyline fill="none" stroke="#409eff" stroke-width="2" :points="historySparkline.polyline" />
            <line
              :x1="0"
              :y1="historySparkline.thresholdY"
              :x2="560"
              :y2="historySparkline.thresholdY"
              stroke="#f56c6c"
              stroke-dasharray="4 4"
            />
          </svg>
          <p v-else class="hint">触发次数不足，无法绘制趋势</p>
        </div>

        <div class="history-summary" data-testid="alert-history-summary">
          <span>最近 100 条 · 共 {{ historyDialog.items.length }} 次触发</span>
          <span>最新触发值 {{ historyLatestTriggerValue }}</span>
          <span>平均触发值 {{ historyAverageTriggerValue }}</span>
          <span>最大触发值 {{ historyMaxTriggerValue }}</span>
        </div>
        <div class="history-severity" data-testid="alert-history-severity">
          <span v-for="item in historySeveritySummary" :key="item.label">{{ item.label }} {{ item.count }}</span>
        </div>
        <el-table :data="historyDialog.items" size="small" max-height="360">
          <el-table-column label="时间" width="150">
            <template #default="{ row }">{{ formatDateTime(row.fired_at) }}</template>
          </el-table-column>
          <el-table-column label="触发值" width="100">
            <template #default="{ row }">{{ row.trigger_value }}</template>
          </el-table-column>
          <el-table-column label="阈值" width="90">
            <template #default="{ row }">{{ row.threshold }}</template>
          </el-table-column>
          <el-table-column label="严重度" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="severityType(row.severity)">{{ row.severity }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="消息" prop="message" />
        </el-table>
        <p v-if="!historyDialog.loading && historyDialog.items.length === 0" class="hint">该规则尚未触发过。</p>
      </div>
      <template #footer>
        <el-button plain :loading="historyDialog.loading" @click="reloadHistory">刷新</el-button>
        <el-button @click="historyDialog.visible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listAlertRules,
  createAlertRule,
  updateAlertRule,
  deleteAlertRule,
  evaluateAlertRules,
  getAlertRuleEffectiveness,
  getAlertRuleHistory,
} from '../api'
import type { AlertRule, AlertRuleCreate, AlertEvaluateResult, AlertRuleEffectiveness, AlertRuleType, AlertSeverity, AlertFiring } from '../types'
import { alertRuleTypeLabel } from '../utils/labels'
import { resolveErrorMessage } from '../utils/error'

const ACCOUNT_WIDE_RULE_TYPES: ReadonlySet<AlertRuleType> = new Set(['consecutive_losses', 'kill_switch_engaged'])

const rules = ref<AlertRule[]>([])
const loading = ref(false)
const evaluating = ref(false)
const evalResult = ref<AlertEvaluateResult | null>(null)
const activeFilter = ref<'all' | 'enabled' | 'disabled' | 'recent-fired' | 'never-fired'>('all')

// Server-backed firing effectiveness (GET /api/alert-rules/effectiveness).
// Covers ALL rules server-side; firing_count is window-scoped while
// last_fired_at / never_fired are all-time semantics. Loading it is read-only
// and never triggers rule evaluation or notification sends.
const effectivenessItems = ref<AlertRuleEffectiveness[]>([])
const effectivenessTotal = ref(0)
const effectivenessLoading = ref(false)
const effectivenessError = ref('')
const effectivenessLoaded = ref(false)

function defaultEffectivenessRange(): [string, string] {
  const to = new Date()
  const from = new Date(to.getTime() - 29 * 24 * 60 * 60 * 1000)
  return [from.toISOString().slice(0, 10), to.toISOString().slice(0, 10)]
}
const effectivenessRange = ref<string[]>(defaultEffectivenessRange())

const effectivenessById = computed(() => {
  const map = new Map<number, AlertRuleEffectiveness>()
  for (const item of effectivenessItems.value) map.set(item.id, item)
  return map
})

const windowFiringTotal = computed(() =>
  effectivenessItems.value.reduce((sum, item) => sum + item.firing_count, 0),
)
const windowFiredRuleCount = computed(() =>
  effectivenessItems.value.filter((item) => item.firing_count > 0).length,
)
const neverFiredRuleCount = computed(() =>
  effectivenessItems.value.filter((item) => item.never_fired).length,
)
const firedRuleCount = computed(() =>
  effectivenessItems.value.filter((item) => !item.never_fired).length,
)

const effectivenessWindowLabel = computed(() => {
  const from = effectivenessRange.value?.[0]
  const to = effectivenessRange.value?.[1]
  if (from || to) {
    return `统计窗口：${from || '最早'} 至 ${to || '最新'}（窗口触发次数按窗口计；从未触发为全量口径）`
  }
  return '统计窗口：全部时间（触发次数为全量计数）'
})

/** Prefer the server-side all-time never_fired flag when effectiveness has
 * been loaded; the local `last_fired_at === null` heuristic can mislabel
 * legacy rules whose firing rows exist but last_fired_at was never
 * backfilled. */
function isRuleNeverFired(rule: AlertRule): boolean {
  const server = effectivenessById.value.get(rule.id)
  if (effectivenessLoaded.value && server) return server.never_fired
  return rule.last_fired_at === null
}

function ruleLastFiredAt(rule: AlertRule): string | null {
  const server = effectivenessById.value.get(rule.id)
  if (effectivenessLoaded.value && server) return server.last_fired_at
  return rule.last_fired_at
}

const historyDialog = reactive({
  visible: false,
  loading: false,
  ruleId: 0,
  ruleName: '',
  items: [] as AlertFiring[],
})

const historySparkline = computed(() => {
  const sorted = [...historyDialog.items].sort(
    (a, b) => new Date(a.fired_at).getTime() - new Date(b.fired_at).getTime(),
  )
  if (sorted.length < 2) {
    return { points: [] as AlertFiring[], polyline: '', thresholdY: 0 }
  }
  const values = sorted.map((i) => Number(i.trigger_value))
  const threshold = Number(sorted[0].threshold)
  const min = Math.min(threshold, ...values)
  const max = Math.max(threshold, ...values)
  const range = max - min || 1
  const width = 560
  const height = 120
  const padding = 20
  const plotHeight = height - padding * 2
  const points = sorted
    .map((item, idx) => {
      const x = (idx / (sorted.length - 1)) * width
      const y = padding + plotHeight - ((Number(item.trigger_value) - min) / range) * plotHeight
      return `${x},${y}`
    })
    .join(' ')
  const thresholdY = padding + plotHeight - ((threshold - min) / range) * plotHeight
  return { points: sorted, polyline: points, thresholdY }
})

const dialog = reactive({
  visible: false,
  saving: false,
  id: 0,
  name: '',
  symbol: '',
  rule_type: 'price_above' as AlertRuleType,
  threshold: 150,
  severity: 'WARNING' as AlertSeverity,
  enabled: true,
  cooldown_seconds: 300,
})

const isAccountWideType = computed(() => isAccountWideRuleType(dialog.rule_type))

// Adjust form defaults only — never persists. Saving stays behind the
// explicit 保存 button regardless of rule type. Fired by the type select's
// change event (user interaction only), so opening the dialog for edit never
// clobbers the loaded rule's values.
function handleRuleTypeChange(next: AlertRuleType) {
  if (isAccountWideRuleType(next)) {
    dialog.symbol = ''
  }
  if (next === 'kill_switch_engaged') {
    dialog.threshold = 1
  } else if (next === 'consecutive_losses') {
    dialog.threshold = 3
  } else if (next === 'daily_loss') {
    dialog.threshold = -500
  } else {
    dialog.threshold = 150
  }
}

const filteredRules = computed(() => {
  const current = rules.value
  if (activeFilter.value === 'enabled') return current.filter((rule) => rule.enabled)
  if (activeFilter.value === 'disabled') return current.filter((rule) => !rule.enabled)
  if (activeFilter.value === 'recent-fired') return current.filter((rule) => !isRuleNeverFired(rule))
  if (activeFilter.value === 'never-fired') return current.filter((rule) => isRuleNeverFired(rule))
  return current
})

const enabledRules = computed(() => rules.value.filter((rule) => rule.enabled))
const disabledRules = computed(() => rules.value.filter((rule) => !rule.enabled))
const recentlyFiredRules = computed(() =>
  [...rules.value]
    .filter((rule) => !isRuleNeverFired(rule) && ruleLastFiredAt(rule) !== null)
    .sort((a, b) => new Date(ruleLastFiredAt(b) ?? '').getTime() - new Date(ruleLastFiredAt(a) ?? '').getTime()),
)

const historyLatestTriggerValue = computed(() => historyDialog.items[0]?.trigger_value ?? '—')
const historyAverageTriggerValue = computed(() => {
  if (!historyDialog.items.length) return '—'
  const values = historyDialog.items.map((item) => item.trigger_value)
  const sum = values.reduce((total, value) => total + value, 0)
  return (sum / values.length).toFixed(2)
})
const historyMaxTriggerValue = computed(() => {
  if (!historyDialog.items.length) return '—'
  return Math.max(...historyDialog.items.map((item) => item.trigger_value)).toFixed(2)
})
const historySeveritySummary = computed(() => {
  const counts = new Map<string, number>()
  for (const item of historyDialog.items) {
    counts.set(item.severity, (counts.get(item.severity) ?? 0) + 1)
  }
  return [...counts.entries()].map(([label, count]) => ({ label, count }))
})

async function loadRules() {
  loading.value = true
  try {
    rules.value = (await listAlertRules()).items
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '加载失败'))
  } finally {
    loading.value = false
  }
}

/** Read-only server aggregate; never evaluates rules or sends notifications. */
async function loadEffectiveness() {
  effectivenessLoading.value = true
  effectivenessError.value = ''
  try {
    const params: { from_date?: string; to_date?: string } = {}
    const range = effectivenessRange.value
    if (range?.[0]) params.from_date = range[0]
    if (range?.[1]) params.to_date = range[1]
    const page = await getAlertRuleEffectiveness(params)
    effectivenessItems.value = page.items
    effectivenessTotal.value = page.total
    effectivenessLoaded.value = true
  } catch (e) {
    effectivenessError.value = resolveErrorMessage(e, '加载触发统计失败')
    effectivenessItems.value = []
    effectivenessTotal.value = 0
  } finally {
    effectivenessLoading.value = false
  }
}

function isAccountWideRuleType(t: AlertRuleType): boolean {
  return ACCOUNT_WIDE_RULE_TYPES.has(t)
}

function ruleSymbolLabel(rule: AlertRule): string {
  if (rule.symbol) return rule.symbol
  return isAccountWideRuleType(rule.rule_type) ? '账户级' : '—'
}

function ruleConditionLabel(rule: AlertRule): string {
  // kill_switch_engaged stores a fixed threshold of 1 as a storage contract;
  // showing it would be noise, so the condition is the label alone.
  if (rule.rule_type === 'kill_switch_engaged') return alertRuleTypeLabel(rule.rule_type)
  return `${alertRuleTypeLabel(rule.rule_type)} ${rule.threshold}`
}

function severityType(s: string): string {
  if (s === 'CRITICAL') return 'danger'
  if (s === 'WARNING') return 'warning'
  return 'info'
}

function formatDateTime(v: string): string {
  return new Date(v).toLocaleString([], { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}

function openCreate() {
  Object.assign(dialog, {
    visible: true, saving: false, id: 0, name: '', symbol: '',
    rule_type: 'price_above', threshold: 150, severity: 'WARNING', enabled: true, cooldown_seconds: 300,
  })
}

function openEdit(row: AlertRule) {
  Object.assign(dialog, {
    visible: true, saving: false, id: row.id, name: row.name, symbol: row.symbol,
    rule_type: row.rule_type, threshold: row.threshold, severity: row.severity,
    enabled: row.enabled, cooldown_seconds: row.cooldown_seconds,
  })
}

function payload(): AlertRuleCreate {
  const accountWide = isAccountWideRuleType(dialog.rule_type)
  return {
    name: dialog.name.trim(),
    // Account-wide rules read the authoritative account state; the backend
    // rejects any non-blank symbol for them.
    symbol: accountWide ? '' : dialog.symbol.trim().toUpperCase(),
    rule_type: dialog.rule_type,
    // kill_switch_engaged has a fixed threshold contract of exactly 1.0.
    threshold: dialog.rule_type === 'kill_switch_engaged' ? 1 : dialog.threshold,
    severity: dialog.severity,
    enabled: dialog.enabled,
    cooldown_seconds: dialog.cooldown_seconds,
  }
}

function validateDialog(): string | null {
  if (!dialog.name.trim()) return '请填写名称'
  if (dialog.rule_type === 'consecutive_losses') {
    if (!Number.isInteger(dialog.threshold) || dialog.threshold < 1) {
      return '连续亏损阈值须为 ≥ 1 的整数'
    }
  }
  if (isAccountWideRuleType(dialog.rule_type) && dialog.symbol.trim() !== '') {
    return '账户级规则标的须留空'
  }
  return null
}

async function save() {
  const invalid = validateDialog()
  if (invalid) {
    ElMessage.warning(invalid)
    return
  }
  dialog.saving = true
  try {
    if (dialog.id) {
      await updateAlertRule(dialog.id, payload())
    } else {
      await createAlertRule(payload())
    }
    dialog.visible = false
    await loadRules()
    await loadEffectiveness()
    ElMessage.success('已保存')
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '保存失败'))
  } finally {
    dialog.saving = false
  }
}

async function remove(id: number) {
  try {
    await ElMessageBox.confirm('删除该告警规则？', '确认', { type: 'warning' })
  } catch {
    return
  }
  try {
    await deleteAlertRule(id)
    await loadRules()
    await loadEffectiveness()
  } catch {
    ElMessage.error('删除失败')
  }
}

const rulesImportInput = ref<HTMLInputElement | null>(null)

/** Export the currently loaded rules as JSON (offline backup). Strips server-
 * side bookkeeping fields (id, last_fired_at, created_at) so the file only
 * carries user-editable config. */
function exportRulesJson() {
  const portable = rules.value.map((r) => ({
    name: r.name,
    symbol: r.symbol,
    rule_type: r.rule_type,
    threshold: r.threshold,
    severity: r.severity,
    enabled: r.enabled,
    cooldown_seconds: r.cooldown_seconds,
  }))
  const blob = new Blob([JSON.stringify(portable, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = 'alert_rules.json'
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  setTimeout(() => URL.revokeObjectURL(url), 1000)
  ElMessage.success(`已导出 ${portable.length} 条规则`)
}

function triggerImportRulesJson() {
  rulesImportInput.value?.click()
}

/** Bulk-create rules from an exported JSON file by reusing createAlertRule
 * per entry. Importing never deletes or overwrites existing rules; failures
 * are reported per-entry without aborting the rest. */
async function handleImportRulesJson(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  let parsed: unknown
  try {
    parsed = JSON.parse(await file.text())
  } catch {
    ElMessage.error('JSON 解析失败')
    return
  }
  if (!Array.isArray(parsed)) {
    ElMessage.error('规则 JSON 必须是数组')
    return
  }
  let created = 0
  let failed = 0
  for (const entry of parsed) {
    if (!entry || typeof entry !== 'object' || typeof (entry as AlertRuleCreate).name !== 'string') {
      failed += 1
      continue
    }
    const e = entry as AlertRuleCreate
    try {
      await createAlertRule({
        name: e.name,
        symbol: (e.symbol ?? '').toUpperCase(),
        rule_type: e.rule_type,
        threshold: e.threshold,
        severity: e.severity,
        enabled: e.enabled ?? true,
        cooldown_seconds: e.cooldown_seconds ?? 300,
      })
      created += 1
    } catch {
      failed += 1
    }
  }
  await loadRules()
  if (failed > 0) {
    ElMessage.warning(`导入完成：成功 ${created}，失败 ${failed}`)
  } else {
    ElMessage.success(`已导入 ${created} 条规则`)
  }
}

async function toggleEnabled(row: AlertRule, value: boolean | string | number) {
  // Build a clean AlertRuleCreate payload — the backend schema is
  // extra="forbid", so spreading the whole row (incl. id/last_fired_at/
  // created_at) would 422. Only the 7 updatable fields are accepted.
  try {
    await updateAlertRule(row.id, {
      name: row.name,
      symbol: row.symbol,
      rule_type: row.rule_type,
      threshold: row.threshold,
      severity: row.severity,
      enabled: !!value,
      cooldown_seconds: row.cooldown_seconds,
    })
    await loadRules()
    await loadEffectiveness()
  } catch {
    ElMessage.error('更新失败')
  }
}

async function evaluate() {
  evaluating.value = true
  try {
    evalResult.value = await evaluateAlertRules()
    ElMessage.success('评估完成')
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '评估失败'))
  } finally {
    evaluating.value = false
  }
}

async function openHistory(row: AlertRule) {
  historyDialog.ruleId = row.id
  historyDialog.ruleName = row.name
  historyDialog.items = []
  historyDialog.visible = true
  await reloadHistory()
}

async function reloadHistory() {
  if (!historyDialog.ruleId) return
  historyDialog.loading = true
  try {
    const page = await getAlertRuleHistory(historyDialog.ruleId, { limit: 100 })
    historyDialog.items = page.items
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '加载触发历史失败'))
  } finally {
    historyDialog.loading = false
  }
}

onMounted(() => {
  loadRules()
  loadEffectiveness()
})
</script>

<style scoped>
.alerts-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  background: #fff;
  min-height: calc(100vh - 120px);
}

.alerts-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.alerts-header h3 {
  margin: 0;
}

.alerts-actions {
  display: flex;
  gap: 8px;
}

.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
}

.summary-card {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 12px;
  background: #fafafa;
}

.summary-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
}

.summary-items,
.history-summary,
.history-severity,
.filter-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.summary-items span,
.history-summary span,
.history-severity span {
  font-size: 12px;
  color: #606266;
}

.summary-empty {
  font-size: 12px;
  color: #909399;
}

.summary-error {
  font-size: 12px;
  color: var(--el-color-danger);
}

.effectiveness-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.effectiveness-section {
  border: 1px solid #ebeef5;
  border-radius: 8px;
  padding: 12px;
}

.effectiveness-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.effectiveness-header h4 {
  margin: 0;
  font-size: 14px;
}

.effectiveness-state {
  padding: 16px;
  text-align: center;
  color: #909399;
  font-size: 13px;
}

.field-hint {
  margin-top: 4px;
  color: #909399;
  font-size: 12px;
  line-height: 1.4;
}

.filter-row {
  align-items: center;
}

.history-summary {
  margin-bottom: 8px;
}

.history-severity {
  margin-bottom: 12px;
}

.responsive-table {
  width: 100%;
}

.eval-note {
  margin: 0;
  color: #6b7280;
  font-size: 13px;
}

.hint {
  margin: 0;
  color: #909399;
  font-size: 12px;
  line-height: 1.5;
}

.hint code {
  background: #f1f3f5;
  padding: 1px 4px;
  border-radius: 3px;
}

.history-chart {
  margin-bottom: 16px;
  border: 1px solid #e1e7f0;
  border-radius: 6px;
  padding: 12px;
  background: #f8fafc;
}

.history-chart-svg {
  display: block;
  width: 100%;
  height: 120px;
}

@media (max-width: 640px) {
  .alerts-header {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
