<template>
  <div>
    <h3>策略配置</h3>

    <el-card style="max-width: 600px; margin-bottom: 20px">
      <div style="display: flex; justify-content: space-between; align-items: center">
        <h4>LLM 智能区间</h4>
        <el-switch
          v-model="llmStatus.enabled"
          active-text="启用"
          inactive-text="禁用"
          @change="toggleLLM"
        />
      </div>
      <div v-if="llmStatus.current_suggestion" style="margin-top: 12px">
        <p>置信度: {{ llmStatus.current_suggestion.confidence_score }}</p>
        <p>建议区间: {{ formatCurrency(llmStatus.current_suggestion.buy_low, form.market) }} ~ {{ formatCurrency(llmStatus.current_suggestion.sell_high, form.market) }}</p>
        <p>分析: {{ llmStatus.current_suggestion.analysis }}</p>
      </div>
      <div v-if="llmStatus.applied_values" style="margin-top: 8px">
        <p>已应用: {{ formatCurrency(llmStatus.applied_values.buy_low, form.market) }} ~ {{ formatCurrency(llmStatus.applied_values.sell_high, form.market) }}</p>
      </div>
      <div v-if="llmStatus.reject_reason" style="margin-top: 8px; color: #f56c6c">
        <p>上次被拒: {{ llmStatus.reject_reason }}</p>
      </div>
      <div style="margin-top: 12px">
        <p style="margin-bottom: 8px">
          刷新间隔：{{ llmStatus.interval_minutes }} 分钟
        </p>
        <p style="margin-bottom: 8px">
          最近成功刷新：{{ formatTime(llmStatus.last_analysis_at) }}
        </p>
        <el-button size="small" :loading="analyzing" @click="triggerAnalyze">
          当前策略重新分析
        </el-button>
        <span v-if="llmStatus.next_analysis_at" style="margin-left: 12px; color: #909399; font-size: 12px">
          下次分析: {{ formatTime(llmStatus.next_analysis_at) }}
        </span>
      </div>
      <div v-if="llmInteractions.length > 0" style="margin-top: 16px">
        <el-divider />
        <h4 style="margin: 0 0 8px">最近 LLM 交互</h4>
        <el-table :data="llmInteractions" size="small" style="width: 100%">
          <el-table-column prop="created_at" label="时间" min-width="150">
            <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column prop="success" label="结果" width="80">
            <template #default="{ row }">
              <el-tag :type="row.success ? 'success' : 'danger'">{{ row.success ? '成功' : '失败' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="order_action" label="动作" width="120" />
          <el-table-column prop="order_status" label="订单" width="110">
            <template #default="{ row }">{{ row.order_status || '-' }}</template>
          </el-table-column>
        </el-table>
      </div>
    </el-card>

    <el-card style="max-width: 600px; margin-bottom: 20px">
      <h4>LLM 预览分析</h4>
      <p style="color: #909399; font-size: 13px; margin-bottom: 12px">
        输入股票代码后预览 LLM 建议区间，确认后再保存到策略。
      </p>
      <el-form :inline="true" @submit.prevent="handlePreview">
        <el-form-item label="股票代码">
          <el-input v-model="previewSymbol" placeholder="例如 AAPL.US" style="width: 180px" />
        </el-form-item>
        <el-form-item label="市场">
          <el-radio-group v-model="previewMarket">
            <el-radio value="US">美股</el-radio>
            <el-radio value="HK">港股</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="做空">
          <el-switch v-model="previewShortSelling" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="previewing" :disabled="!previewSymbol.trim()" @click="handlePreview">
            预览分析
          </el-button>
        </el-form-item>
      </el-form>

      <div v-if="previewResult" style="margin-top: 16px">
        <el-alert
          :title="previewResult.success ? 'LLM 建议区间' : '分析失败'"
          :type="previewResult.success ? 'success' : 'error'"
          :closable="false"
          show-icon
        >
          <template v-if="previewResult.success">
            <p>置信度: {{ previewResult.confidence_score ?? '-' }}</p>
            <p v-if="previewResult.suggested_buy_low != null">建议买入价: {{ previewResult.suggested_buy_low.toFixed(2) }}</p>
            <p v-if="previewResult.suggested_sell_high != null">建议卖出价: {{ previewResult.suggested_sell_high.toFixed(2) }}</p>
            <p v-if="previewResult.analysis">分析: {{ previewResult.analysis }}</p>
          </template>
          <template v-else>
            <p>{{ previewResult.reason }}</p>
          </template>
        </el-alert>
        <div v-if="canApplyPreview" style="margin-top: 12px; text-align: right">
          <el-button type="success" :loading="savingPreview" @click="applyPreview">
            应用到策略并保存
          </el-button>
        </div>
      </div>
      <div v-if="previewError" style="margin-top: 12px">
        <el-alert :title="previewError" type="error" :closable="false" show-icon />
      </div>
    </el-card>

    <el-card style="max-width: 600px" v-loading="loading">
      <el-form data-testid="strategy-config-form" :model="form" label-width="180px" @submit.prevent="save" :disabled="loading">
        <el-form-item label="股票代码">
          <el-input v-model="form.symbol" placeholder="例如 AAPL.US" />
        </el-form-item>
        <el-form-item label="市场">
          <el-radio-group v-model="form.market">
            <el-radio value="US">美股</el-radio>
            <el-radio value="HK">港股</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="买入价下限">
          <el-input-number v-model="form.buy_low" :precision="2" :step="0.01" />
        </el-form-item>
        <el-form-item label="卖出价上限">
          <el-input-number v-model="form.sell_high" :precision="2" :step="0.01" />
        </el-form-item>
        <el-form-item label="做空">
          <el-switch v-model="form.short_selling" />
        </el-form-item>
        <el-form-item label="单笔最低盈利金额">
          <el-input-number v-model="form.min_profit_amount" :min="0" :precision="2" :step="0.01" />
        </el-form-item>
        <el-form-item label="暂停自动恢复（分钟）">
          <el-input-number v-model="form.auto_resume_minutes" :min="0" :max="1440" :step="1" />
        </el-form-item>
        <el-form-item label="单日最大亏损">
          <el-input-number v-model="form.max_daily_loss" :min="1" :precision="2" />
        </el-form-item>
        <el-form-item label="连续亏损暂停阈值">
          <el-input-number v-model="form.max_consecutive_losses" :min="1" />
        </el-form-item>
        <el-form-item label="LLM刷新间隔（分钟）">
          <el-input-number v-model="form.llm_interval_minutes" :min="1" :max="1440" :step="1" />
        </el-form-item>
        <el-divider content-position="left">成本与执行保护</el-divider>
        <el-form-item label="美股单边预估费率（%）">
          <el-input-number v-model="form.fee_rate_us" :min="0" :max="1" :precision="3" :step="0.01" />
        </el-form-item>
        <el-form-item label="港股单边预估费率（%）">
          <el-input-number v-model="form.fee_rate_hk" :min="0" :max="2" :precision="3" :step="0.01" />
        </el-form-item>
        <el-form-item label="LLM 最小改价（%）">
          <el-input-number v-model="form.min_repricing_pct" :min="0" :max="5" :precision="3" :step="0.01" />
        </el-form-item>
        <el-form-item label="LLM 同向冷却（秒）">
          <el-input-number v-model="form.llm_action_cooldown_seconds" :min="0" :max="3600" :step="1" />
        </el-form-item>
        <el-form-item label="买入保证金安全系数">
          <el-input-number v-model="form.margin_safety_factor" :min="0" :max="1" :precision="2" :step="0.05" />
          <div style="margin-left: 8px; color: #909399; font-size: 12px;">
            从券商获取的最大买入量的乘数。0.9 = 使用 90% 的保证金购买力。
          </div>
        </el-form-item>
        <el-divider content-position="left">交易时段</el-divider>
        <el-form-item label="新单时段">
          <el-radio-group v-model="form.trading_session_mode" data-testid="trading-session-mode">
            <el-radio value="ANY">任意时段</el-radio>
            <el-radio value="RTH_ONLY">仅常规交易时段（RTH）</el-radio>
          </el-radio-group>
          <div style="margin-top: 8px; color: #909399; font-size: 12px; line-height: 1.4">
            RTH 仅按工作日 + 开市窗口判断，<strong>不含交易所节假日</strong>；设为「任意」则与旧版行为一致。
          </div>
        </el-form-item>
        <el-divider content-position="left">定时报告</el-divider>
        <el-form-item label="启用定时报告">
          <el-switch v-model="form.report_schedule_enabled" data-testid="report-schedule-enabled" />
          <span style="margin-left: 10px; color: #909399; font-size: 12px">按周期把日报推送到已配置的通知渠道</span>
        </el-form-item>
        <el-form-item label="报告标的">
          <el-input v-model="form.report_schedule_symbol" placeholder="留空则用当前策略标的" data-testid="report-schedule-symbol" />
        </el-form-item>
        <el-form-item label="推送间隔（小时）">
          <el-input-number v-model="form.report_schedule_interval_hours" :min="1" :max="720" :step="1" data-testid="report-schedule-interval" />
        </el-form-item>
        <el-form-item label="立即发送测试">
          <el-button plain :loading="reportSending" data-testid="report-schedule-test" @click="sendReportNow">发送一次</el-button>
          <span v-if="reportSendResult" style="margin-left: 10px; font-size: 12px" :style="{ color: reportSendResult.sent ? '#14884f' : '#c43838' }">
            {{ reportSendResult.sent ? '已发送' : '发送失败' }}{{ reportSendResult.error ? `：${reportSendResult.error}` : '' }}
          </span>
        </el-form-item>
        <el-divider content-position="left">参数预设</el-divider>
        <el-form-item label="存为预设">
          <el-input v-model="presetName" placeholder="如：保守 / 激进" style="max-width: 200px" data-testid="preset-name" />
          <el-button type="primary" plain style="margin-left: 8px" :loading="presetBusy" :disabled="!presetName.trim()" data-testid="preset-save" @click="savePreset">存为预设</el-button>
          <span style="margin-left: 8px; color: #909399; font-size: 12px">保存当前表单为命名快照</span>
        </el-form-item>
        <el-form-item v-if="presets.length" label="应用预设">
          <el-select v-model="selectedPresetId" placeholder="选择预设" style="max-width: 200px" data-testid="preset-select">
            <el-option v-for="p in presets" :key="p.id" :label="p.name" :value="p.id" />
          </el-select>
          <el-button type="primary" plain style="margin-left: 8px" :disabled="!selectedPresetId" :loading="presetBusy" data-testid="preset-apply" @click="selectedPresetId && applyPreset(selectedPresetId)">应用</el-button>
          <el-button plain :disabled="!selectedPresetId" :loading="presetBusy" data-testid="preset-delete" @click="selectedPresetId && removePreset(selectedPresetId)">删除</el-button>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" native-type="submit" :loading="saving" :disabled="loading || !isDirty">保存</el-button>
          <el-tag v-if="saved" type="success" style="margin-left: 10px">已保存</el-tag>
          <el-tag v-if="error" type="danger" style="margin-left: 10px">{{ error }}</el-tag>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import { onBeforeRouteLeave, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getStrategy, updateStrategy, getLLMIntervalStatus, analyzeLLMInterval, previewLLMInterval, enableLLMInterval, disableLLMInterval, getLLMInteractions, listStrategyPresets, createStrategyPreset, deleteStrategyPreset, applyStrategyPreset } from '../api'
import { runScheduledReportNow } from '../api/reports'
import { useFormState } from '../composables/useFormState'
import type { LLMIntervalStatus, LLMAnalyzeResponse, LLMInteractionRecord, StrategyPreset } from '../types'
import { formatCurrency } from '../utils/format'

interface StrategyForm {
  symbol: string
  market: 'US' | 'HK'
  buy_low: number
  sell_high: number
  short_selling: boolean
  min_profit_amount: number
  auto_resume_minutes: number
  max_daily_loss: number
  max_consecutive_losses: number
  llm_interval_minutes: number
  fee_rate_us: number
  fee_rate_hk: number
  min_repricing_pct: number
  llm_action_cooldown_seconds: number
  trading_session_mode: 'ANY' | 'RTH_ONLY'
  margin_safety_factor: number
  report_schedule_enabled: boolean
  report_schedule_interval_hours: number
  report_schedule_symbol: string
}

const loadedStrategy = ref<StrategyForm | null>(null)

const { form, loading, saving, saved, error, isDirty, load, save } = useFormState({
  initial: {
    symbol: '',
    market: 'US' as 'US' | 'HK',
    buy_low: 0,
    sell_high: 0,
    short_selling: false,
    min_profit_amount: 0,
    auto_resume_minutes: 3,
    max_daily_loss: 5000,
    max_consecutive_losses: 3,
    llm_interval_minutes: 2,
    fee_rate_us: 0.05,
    fee_rate_hk: 0.30,
    min_repricing_pct: 0.30,
    llm_action_cooldown_seconds: 60,
    margin_safety_factor: 0.9,
    trading_session_mode: 'ANY',
    report_schedule_enabled: false,
    report_schedule_interval_hours: 24,
    report_schedule_symbol: '',
  },
  load: async () => {
    const s = await getStrategy()
    const loaded: StrategyForm = {
      symbol: s.symbol,
      market: s.market,
      buy_low: s.buy_low,
      sell_high: s.sell_high,
      short_selling: s.short_selling,
      min_profit_amount: s.min_profit_amount,
      auto_resume_minutes: s.auto_resume_minutes,
      max_daily_loss: s.max_daily_loss,
      max_consecutive_losses: s.max_consecutive_losses,
      llm_interval_minutes: s.llm_interval_minutes,
      fee_rate_us: s.fee_rate_us * 100,
      fee_rate_hk: s.fee_rate_hk * 100,
      min_repricing_pct: s.min_repricing_pct * 100,
      llm_action_cooldown_seconds: s.llm_action_cooldown_seconds,
      margin_safety_factor: s.margin_safety_factor ?? 0.9,
      trading_session_mode: s.trading_session_mode === 'RTH_ONLY' ? 'RTH_ONLY' : 'ANY',
      report_schedule_enabled: s.report_schedule_enabled ?? false,
      report_schedule_interval_hours: s.report_schedule_interval_hours ?? 24,
      report_schedule_symbol: s.report_schedule_symbol ?? '',
    }
    loadedStrategy.value = loaded
    return loaded
  },
  save: async (data) => {
    const patch: Parameters<typeof updateStrategy>[0] = {}
    const previous = loadedStrategy.value
    if (!previous || data.symbol !== previous.symbol) patch.symbol = data.symbol
    if (!previous || data.market !== previous.market) patch.market = data.market
    if (!previous || data.buy_low !== previous.buy_low) patch.buy_low = data.buy_low
    if (!previous || data.sell_high !== previous.sell_high) patch.sell_high = data.sell_high
    if (!previous || data.short_selling !== previous.short_selling) patch.short_selling = data.short_selling
    if (!previous || data.min_profit_amount !== previous.min_profit_amount) patch.min_profit_amount = data.min_profit_amount
    if (!previous || data.auto_resume_minutes !== previous.auto_resume_minutes) patch.auto_resume_minutes = data.auto_resume_minutes
    if (!previous || data.max_daily_loss !== previous.max_daily_loss) patch.max_daily_loss = data.max_daily_loss
    if (!previous || data.max_consecutive_losses !== previous.max_consecutive_losses) patch.max_consecutive_losses = data.max_consecutive_losses
    if (!previous || data.llm_interval_minutes !== previous.llm_interval_minutes) patch.llm_interval_minutes = data.llm_interval_minutes
    if (!previous || data.fee_rate_us !== previous.fee_rate_us) patch.fee_rate_us = data.fee_rate_us / 100
    if (!previous || data.fee_rate_hk !== previous.fee_rate_hk) patch.fee_rate_hk = data.fee_rate_hk / 100
    if (!previous || data.min_repricing_pct !== previous.min_repricing_pct) patch.min_repricing_pct = data.min_repricing_pct / 100
    if (!previous || data.llm_action_cooldown_seconds !== previous.llm_action_cooldown_seconds) {
      patch.llm_action_cooldown_seconds = data.llm_action_cooldown_seconds
    }
    if (!previous || data.trading_session_mode !== previous.trading_session_mode) {
      patch.trading_session_mode = data.trading_session_mode === 'RTH_ONLY' ? 'RTH_ONLY' : 'ANY'
    }
    if (!previous || data.margin_safety_factor !== previous.margin_safety_factor) patch.margin_safety_factor = data.margin_safety_factor
    if (!previous || data.report_schedule_enabled !== previous.report_schedule_enabled) patch.report_schedule_enabled = data.report_schedule_enabled
    if (!previous || data.report_schedule_interval_hours !== previous.report_schedule_interval_hours) patch.report_schedule_interval_hours = data.report_schedule_interval_hours
    if (!previous || data.report_schedule_symbol !== previous.report_schedule_symbol) patch.report_schedule_symbol = data.report_schedule_symbol
    if (Object.keys(patch).length === 0) return
    await updateStrategy(patch)
    loadedStrategy.value = {
      ...data,
      trading_session_mode: data.trading_session_mode === 'RTH_ONLY' ? 'RTH_ONLY' : 'ANY',
    }
    await loadLLMStatus()
  },
})

const reportSending = ref(false)
const reportSendResult = ref<{ sent: boolean; error: string | null } | null>(null)

async function sendReportNow() {
  reportSending.value = true
  reportSendResult.value = null
  try {
    const res = await runScheduledReportNow()
    reportSendResult.value = { sent: res.sent, error: res.error }
    if (res.sent) ElMessage.success('定时报告已发送')
    else ElMessage.warning('发送失败，请检查通知渠道配置')
  } catch (e) {
    reportSendResult.value = { sent: false, error: '请求失败' }
    ElMessage.error('发送请求失败')
  } finally {
    reportSending.value = false
  }
}

// ---- Strategy presets ----
const presets = ref<StrategyPreset[]>([])
const presetName = ref('')
const selectedPresetId = ref<number | null>(null)
const presetBusy = ref(false)

function buildPresetParams(): Record<string, unknown> {
  return {
    symbol: form.value.symbol,
    market: form.value.market,
    buy_low: form.value.buy_low,
    sell_high: form.value.sell_high,
    short_selling: form.value.short_selling,
    min_profit_amount: form.value.min_profit_amount,
    auto_resume_minutes: form.value.auto_resume_minutes,
    max_daily_loss: form.value.max_daily_loss,
    max_consecutive_losses: form.value.max_consecutive_losses,
    fee_rate_us: form.value.fee_rate_us / 100,
    fee_rate_hk: form.value.fee_rate_hk / 100,
    min_repricing_pct: form.value.min_repricing_pct / 100,
    llm_action_cooldown_seconds: form.value.llm_action_cooldown_seconds,
    trading_session_mode: form.value.trading_session_mode,
    margin_safety_factor: form.value.margin_safety_factor,
  }
}

async function loadPresets() {
  try {
    presets.value = (await listStrategyPresets()).items
  } catch {
    // non-fatal
  }
}

async function savePreset() {
  if (!presetName.value.trim()) {
    ElMessage.warning('请填写预设名称')
    return
  }
  presetBusy.value = true
  try {
    await createStrategyPreset({ name: presetName.value.trim(), params: buildPresetParams() })
    presetName.value = ''
    await loadPresets()
    ElMessage.success('预设已保存')
  } catch {
    ElMessage.error('保存预设失败')
  } finally {
    presetBusy.value = false
  }
}

async function applyPreset(id: number) {
  presetBusy.value = true
  try {
    const res = await applyStrategyPreset(id)
    ElMessage.success(`已应用预设（变更 ${res.changed.length} 项）`)
    await load()
  } catch {
    ElMessage.error('应用预设失败')
  } finally {
    presetBusy.value = false
  }
}

async function removePreset(id: number) {
  try {
    await ElMessageBox.confirm('删除该预设？', '确认', { type: 'warning' })
  } catch {
    return
  }
  try {
    await deleteStrategyPreset(id)
    await loadPresets()
  } catch {
    ElMessage.error('删除失败')
  }
}

const llmStatus = ref<LLMIntervalStatus>({
  enabled: false,
  interval_minutes: 2,
  last_analysis_at: null,
  next_analysis_at: null,
  current_suggestion: null,
  applied_values: null,
  reject_reason: null,
  budget: {
    max_symbols_per_cycle: 0,
    max_analyses_per_hour: 0,
    tracked_symbol_count: 0,
    effective_symbol_budget: 0,
    used_analyses_last_hour: 0,
    remaining_analyses_this_hour: 0,
  },
  symbol_statuses: [],
})
const llmInteractions = ref<LLMInteractionRecord[]>([])

const analyzing = ref(false)

const previewSymbol = ref('')
const previewMarket = ref<'US' | 'HK'>('US')
const previewShortSelling = ref(false)
const previewing = ref(false)
const previewResult = ref<LLMAnalyzeResponse | null>(null)
const previewError = ref<string | null>(null)
const savingPreview = ref(false)

const canApplyPreview = computed(() => (
  previewResult.value?.success === true
  && previewResult.value.suggested_buy_low != null
  && previewResult.value.suggested_sell_high != null
))

const loadLLMStatus = async () => {
  try {
    llmStatus.value = await getLLMIntervalStatus()
  } catch {
    // silent
  }
}

const loadLLMInteractions = async () => {
  try {
    llmInteractions.value = await getLLMInteractions(10)
  } catch {
    llmInteractions.value = []
  }
}

const toggleLLM = async (val: boolean) => {
  try {
    if (val) {
      await enableLLMInterval()
    } else {
      await disableLLMInterval()
    }
    ElMessage.success(val ? 'LLM 智能区间已启用' : 'LLM 智能区间已禁用')
    await loadLLMStatus()
  } catch {
    ElMessage.error('操作失败')
    llmStatus.value.enabled = !val
  }
}

const triggerAnalyze = async () => {
  analyzing.value = true
  try {
    const result = await analyzeLLMInterval(true)
    if (result.success) {
      ElMessage.success('分析完成')
      if (result.order_action && result.order_action !== 'NONE') {
        ElMessage.info(`LLM 动作: ${result.order_action} / ${result.order_status || '未执行'}`)
      }
      if (result.applied) {
        ElMessage.success(`已应用新区间: ${result.suggested_buy_low?.toFixed(2)} ~ ${result.suggested_sell_high?.toFixed(2)}`)
        await load()
      } else {
        ElMessage.info(result.reason)
      }
    } else {
      ElMessage.warning(result.reason)
    }
    await loadLLMStatus()
    await loadLLMInteractions()
  } catch {
    ElMessage.error('分析失败')
  } finally {
    analyzing.value = false
  }
}

const handlePreview = async () => {
  const symbol = previewSymbol.value.trim()
  if (!symbol) return

  previewing.value = true
  previewResult.value = null
  previewError.value = null
  try {
    const result = await previewLLMInterval({
      symbol,
      market: previewMarket.value,
      current_buy_low: form.value.buy_low,
      current_sell_high: form.value.sell_high,
      min_profit_amount: form.value.min_profit_amount,
      short_selling: previewShortSelling.value,
    })
    previewResult.value = result
  } catch {
    previewError.value = '预览分析请求失败'
  } finally {
    previewing.value = false
  }
}

const applyPreview = async () => {
  if (!canApplyPreview.value || !previewResult.value) return
  const suggestedBuyLow = previewResult.value.suggested_buy_low
  const suggestedSellHigh = previewResult.value.suggested_sell_high
  if (suggestedBuyLow == null || suggestedSellHigh == null) return

  savingPreview.value = true
  try {
    form.value.symbol = previewSymbol.value.trim()
    form.value.buy_low = suggestedBuyLow
    form.value.sell_high = suggestedSellHigh
    form.value.market = previewMarket.value
    form.value.short_selling = previewShortSelling.value
    await save()
    if (error.value) {
      ElMessage.error('保存失败')
      return
    }
    ElMessage.success('已将 LLM 建议应用到策略并保存')
    previewResult.value = null
    await loadLLMStatus()
  } catch {
    ElMessage.error('保存失败')
  } finally {
    savingPreview.value = false
  }
}

const formatTime = (iso: string | null) => {
  if (!iso) return '-'
  const date = new Date(iso)
  // Guard against malformed strings (`new Date('garbage')` returns a Date
  // object whose getTime() is NaN; toLocaleString would then yield the
  // platform-specific "Invalid Date" string).
  if (isNaN(date.getTime())) return '-'
  return date.toLocaleString('zh-CN')
}

const route = useRoute()

onMounted(async () => {
  await load()
  loadPresets()
  // Apply draft experiment run parameters from query string after load() completes
  const draftRunId = route.query.draftExperimentRunId
  if (draftRunId) {
    const qb = route.query.buy_low
    const qs = route.query.sell_high
    const qf = route.query.fee_rate
    const qMarket = route.query.market
    // Reject empty strings and the literal '0' — `Number('0')` is `0` and
    // would silently clobber the loaded strategy value.
    const isMeaningful = (v: unknown): v is string | number =>
      v != null && v !== '' && String(v) !== '0'
    if (isMeaningful(qb)) {
      const parsed = Number(qb)
      if (!isNaN(parsed) && parsed !== 0) form.value.buy_low = parsed
    }
    if (isMeaningful(qs)) {
      const parsed = Number(qs)
      if (!isNaN(parsed) && parsed !== 0) form.value.sell_high = parsed
    }
    if (isMeaningful(qf)) {
      const parsedFee = Number(qf)
      if (!isNaN(parsedFee) && parsedFee !== 0) {
        if (qMarket === 'HK') {
          form.value.fee_rate_hk = parsedFee * 100
        } else {
          form.value.fee_rate_us = parsedFee * 100
        }
      }
    }
    ElMessage.info(`已加载实验 Run #${draftRunId} 的草稿参数，请确认后保存`)
  }
  loadLLMStatus()
  loadLLMInteractions()
})

onBeforeRouteLeave(() => {
  if (!isDirty.value) return true
  return ElMessageBox.confirm('策略配置尚未保存，确定要离开当前页面吗？', '未保存的更改', { type: 'warning' })
    .then(() => true)
    .catch(() => false)
})
</script>

<style scoped>
@media (max-width: 520px) {
  :deep(.el-form-item__label) {
    float: none;
    display: block;
    text-align: left;
    padding: 0 0 4px;
    line-height: 1.4;
  }

  :deep(.el-form-item__content) {
    margin-left: 0 !important;
  }

  :deep(.el-form-item) {
    margin-bottom: 14px;
  }

  :deep(.el-input__wrapper),
  :deep(.el-input-number),
  :deep(.el-select),
  :deep(.el-radio-group) {
    width: 100% !important;
  }
}
</style>
