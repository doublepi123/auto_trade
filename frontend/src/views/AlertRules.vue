<template>
  <div class="alerts-page">
    <div class="alerts-header">
      <h3>告警规则</h3>
      <div class="alerts-actions">
        <el-button :loading="loading" @click="loadRules">刷新</el-button>
        <el-button type="primary" plain :loading="evaluating" data-testid="alert-evaluate" @click="evaluate">立即评估</el-button>
        <el-button type="primary" data-testid="alert-create" @click="openCreate">新建规则</el-button>
      </div>
    </div>

    <p v-if="evalResult" class="eval-note">
      已评估 {{ evalResult.evaluated }} 条，触发 {{ evalResult.fired }} 条（{{ evalResult.skipped_cooldown }} 条冷却中跳过）。
    </p>
    <p class="hint">
      后台每 60 秒评估一次启用规则：价格类用实时行情，日内亏损类读 <code>runtime_state.daily_pnl</code>；触发后按冷却时间节流，经已配置通知渠道推送。
    </p>

    <el-table :data="rules" size="small" class="responsive-table" v-loading="loading">
      <el-table-column prop="name" label="名称" min-width="120" />
      <el-table-column prop="symbol" label="标的" min-width="90" />
      <el-table-column label="条件" min-width="130">
        <template #default="{ row }">{{ ruleTypeLabel(row.rule_type) }} {{ row.threshold }}</template>
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

    <el-dialog v-model="dialog.visible" :title="dialog.id ? '编辑规则' : '新建规则'" width="480px" data-testid="alert-dialog">
      <el-form label-width="90px">
        <el-form-item label="名称">
          <el-input v-model="dialog.name" placeholder="规则名称" data-testid="alert-name" />
        </el-form-item>
        <el-form-item label="标的">
          <el-input v-model="dialog.symbol" placeholder="AAPL.US" />
        </el-form-item>
        <el-form-item label="类型">
          <el-select v-model="dialog.rule_type" data-testid="alert-rule-type" style="width: 100%">
            <el-option label="价格上穿 ≥" value="price_above" />
            <el-option label="价格下穿 ≤" value="price_below" />
            <el-option label="日内亏损 ≤" value="daily_loss" />
          </el-select>
        </el-form-item>
        <el-form-item label="阈值">
          <el-input-number v-model="dialog.threshold" :precision="2" :step="1" />
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
  getAlertRuleHistory,
} from '../api'
import type { AlertRule, AlertRuleCreate, AlertEvaluateResult, AlertRuleType, AlertSeverity, AlertFiring } from '../types'
import { resolveErrorMessage } from '../utils/error'

const rules = ref<AlertRule[]>([])
const loading = ref(false)
const evaluating = ref(false)
const evalResult = ref<AlertEvaluateResult | null>(null)

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

function ruleTypeLabel(t: string): string {
  if (t === 'price_above') return '价格上穿 ≥'
  if (t === 'price_below') return '价格下穿 ≤'
  if (t === 'daily_loss') return '日内亏损 ≤'
  return t
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
  return {
    name: dialog.name.trim(),
    symbol: dialog.symbol.trim().toUpperCase(),
    rule_type: dialog.rule_type,
    threshold: dialog.threshold,
    severity: dialog.severity,
    enabled: dialog.enabled,
    cooldown_seconds: dialog.cooldown_seconds,
  }
}

async function save() {
  if (!dialog.name.trim()) {
    ElMessage.warning('请填写名称')
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
  } catch {
    ElMessage.error('删除失败')
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

onMounted(loadRules)
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
