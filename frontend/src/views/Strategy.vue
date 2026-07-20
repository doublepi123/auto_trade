<template>
  <div>
    <h3>策略配置</h3>

    <el-card style="max-width: 600px; margin-bottom: 20px">
      <div style="display: flex; justify-content: space-between; align-items: center">
        <h4>LLM 智能区间</h4>
        <div style="display: flex; align-items: center; gap: 10px">
          <el-tag data-testid="llm-policy-mode" :type="llmStatus.shadow_mode ? 'warning' : 'success'" effect="plain">
            {{ llmStatus.shadow_mode ? '影子观察' : '实盘应用' }}
          </el-tag>
          <el-switch
            v-model="llmStatus.enabled"
            active-text="启用"
            inactive-text="禁用"
            @change="toggleLLM"
          />
        </div>
      </div>
      <div v-if="llmStatus.current_suggestion" style="margin-top: 12px">
        <p>置信度: {{ llmStatus.current_suggestion.confidence_score }}</p>
        <p>建议区间: {{ formatCurrency(llmStatus.current_suggestion.buy_low, form.market) }} ~ {{ formatCurrency(llmStatus.current_suggestion.sell_high, form.market) }}</p>
        <p>分析: {{ llmStatus.current_suggestion.analysis }}</p>
      </div>
      <div v-if="llmStatus.applied_values" style="margin-top: 8px">
        <p>已应用: {{ formatCurrency(llmStatus.applied_values.buy_low, form.market) }} ~ {{ formatCurrency(llmStatus.applied_values.sell_high, form.market) }}</p>
      </div>
      <div v-else-if="llmStatus.shadow_mode && llmStatus.last_applied_values" style="margin-top: 8px; color: #909399">
        <p>历史实盘应用: {{ formatCurrency(llmStatus.last_applied_values.buy_low, form.market) }} ~ {{ formatCurrency(llmStatus.last_applied_values.sell_high, form.market) }}</p>
      </div>
      <div v-if="llmStatus.reject_reason" style="margin-top: 8px; color: #f56c6c">
        <p>上次被拒: {{ llmStatus.reject_reason }}</p>
      </div>
      <div v-if="llmConsistencyHint" style="margin-top: 8px" data-testid="llm-consistency-hint">
        <el-tag :type="llmConsistencyHint.type" size="small" effect="plain">{{ llmConsistencyHint.text }}</el-tag>
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
        <div style="display: flex; align-items: center; justify-content: space-between; margin: 0 0 8px">
          <h4 style="margin: 0">最近 LLM 交互</h4>
          <div>
            <el-tag size="small" type="info" data-testid="llm-interaction-summary">
              成功率 {{ llmInteractionSuccessRate.toFixed(0) }}% ({{ llmInteractionSuccessCount }}/{{ llmInteractions.length }})
            </el-tag>
            <el-button size="small" link data-testid="llm-interactions-export" @click="exportLLMInteractions">导出 CSV</el-button>
          </div>
        </div>
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
          <el-switch v-model="previewShortSelling" disabled />
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
        <el-alert
          v-if="previewResult.success && llmStatus.shadow_mode"
          title="影子观察模式仅记录建议，不会写入实盘策略"
          type="warning"
          :closable="false"
          show-icon
          style="margin-top: 12px"
        />
      </div>
      <div v-if="previewError" style="margin-top: 12px">
        <el-alert :title="previewError" type="error" :closable="false" show-icon />
      </div>
    </el-card>

    <el-card style="max-width: 600px" v-loading="loading">
      <el-form data-testid="strategy-config-form" :model="form" label-width="180px" @submit.prevent="save" :disabled="loading">
        <el-form-item label="股票代码">
          <el-input v-model="form.symbol" placeholder="例如 AAPL.US" data-testid="strategy-symbol" />
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
        <el-form-item v-if="rangeReadout" label="区间毛利估算">
          <div data-testid="range-readout" class="range-readout">
            <span>价差 {{ formatCurrency(rangeReadout.spread, form.market) }}</span>
            <span>· 估算往返费用 {{ formatCurrency(rangeReadout.estRoundTripFee, form.market) }}</span>
            <span :class="rangeReadout.net >= 0 ? 'positive' : 'negative'">
              · 净利 {{ formatCurrency(rangeReadout.net, form.market) }}
            </span>
          </div>
        </el-form-item>
        <el-form-item label="做空">
          <el-switch v-model="form.short_selling" disabled />
        </el-form-item>
        <el-form-item label="单笔最低盈利金额">
          <el-input-number v-model="form.min_profit_amount" :min="0" :precision="2" :step="0.01" />
        </el-form-item>
        <el-form-item label="暂停自动恢复（分钟）">
          <el-input-number v-model="form.auto_resume_minutes" :min="0" :max="1440" :step="1" />
        </el-form-item>
        <el-form-item label="单日最大亏损">
          <el-input-number v-model="form.max_daily_loss" :min="1" :precision="2" :step="0.01" />
        </el-form-item>
        <el-form-item label="最大回撤额度">
          <el-input-number v-model="form.max_drawdown_amount" :min="0" :precision="2" :step="0.01" :value-on-clear="null" data-testid="max-drawdown-amount" />
        </el-form-item>
        <el-form-item label="连续亏损暂停阈值">
          <el-input-number v-model="form.max_consecutive_losses" :min="1" />
        </el-form-item>
        <el-form-item label="LLM刷新间隔（分钟）">
          <el-input-number v-model="form.llm_interval_minutes" :min="1" :max="1440" :step="1" />
        </el-form-item>
        <el-divider content-position="left">成本与执行保护</el-divider>
        <el-form-item label="美股单边预估费率（%）">
          <el-input-number v-model="form.fee_rate_us" :min="0" :max="1" :precision="3" :step="0.001" />
        </el-form-item>
        <el-form-item label="港股单边预估费率（%）">
          <el-input-number v-model="form.fee_rate_hk" :min="0" :max="2" :precision="3" :step="0.001" />
        </el-form-item>
        <el-form-item label="LLM 最小改价（%）">
          <el-input-number v-model="form.min_repricing_pct" :min="0" :max="5" :precision="3" :step="0.001" />
        </el-form-item>
        <el-form-item label="LLM 同向冷却（秒）">
          <el-input-number v-model="form.llm_action_cooldown_seconds" :min="0" :max="3600" :step="1" />
        </el-form-item>
        <el-form-item label="买入保证金安全系数">
          <el-input-number v-model="form.margin_safety_factor" :min="0" :max="1" :precision="2" :step="0.01" />
          <div style="margin-left: 8px; color: #909399; font-size: 12px;">
            从券商获取的最大买入量的乘数。0.9 = 使用 90% 的保证金购买力。
          </div>
        </el-form-item>
        <el-divider content-position="left">P0 实盘安全边界</el-divider>
        <el-form-item label="允许持仓加仓">
          <el-switch v-model="form.allow_position_addons" disabled data-testid="allow-position-addons" />
        </el-form-item>
        <el-form-item label="最大持仓数量">
          <el-input-number v-model="form.max_position_quantity" :min="1" :max="100" :step="1" data-testid="max-position-quantity" />
        </el-form-item>
        <el-form-item label="最大仓位名义金额">
          <el-input-number v-model="form.max_position_notional" :min="1" :max="5000" :precision="2" :step="0.01" data-testid="max-position-notional" />
        </el-form-item>
        <el-form-item label="单笔最大风险金额">
          <el-input-number v-model="form.max_risk_per_trade" :min="1" :max="250" :precision="2" :step="0.01" data-testid="max-risk-per-trade" />
        </el-form-item>
        <el-form-item label="硬止损（%）">
          <el-input-number v-model="form.stop_loss_pct" :min="0.01" :max="1" :precision="2" :step="0.01" data-testid="stop-loss-pct" />
        </el-form-item>
        <el-form-item label="最长持仓（分钟）">
          <el-input-number v-model="form.max_holding_minutes" :min="1" :max="60" :step="1" data-testid="max-holding-minutes" />
        </el-form-item>
        <el-form-item label="收盘前停止开仓（分钟）">
          <el-input-number v-model="form.entry_cutoff_minutes_before_close" :min="45" :max="180" :step="5" data-testid="entry-cutoff-minutes" />
        </el-form-item>
        <el-form-item label="收盘前清仓（分钟）">
          <el-input-number v-model="form.flatten_minutes_before_close" :min="15" :max="form.entry_cutoff_minutes_before_close" :step="5" data-testid="flatten-minutes" />
        </el-form-item>
        <el-form-item label="LLM 实盘订单">
          <el-switch v-model="form.llm_order_execution_enabled" disabled data-testid="llm-order-execution" />
        </el-form-item>
        <el-divider content-position="left">交易时段</el-divider>
        <el-form-item label="新单时段">
          <el-radio-group v-model="form.trading_session_mode" data-testid="trading-session-mode">
            <el-radio value="ANY">任意时段</el-radio>
            <el-radio value="RTH_ONLY">仅常规交易时段（RTH）</el-radio>
          </el-radio-group>
          <div style="margin-top: 8px; color: #909399; font-size: 12px; line-height: 1.4">
            RTH 按交易所开市日、半日市和常规交易窗口判断；设为「任意」则与旧版行为一致。
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
        <el-form-item label="JSON 预设">
          <el-button plain data-testid="preset-export-current" @click="exportCurrentPreset">导出当前参数</el-button>
          <el-button plain data-testid="preset-import" @click="triggerImport">导入 JSON</el-button>
          <input ref="importInput" type="file" accept=".json,application/json" style="display: none" data-testid="preset-import-input" @change="handleImport" />
          <span style="margin-left: 8px; color: #909399; font-size: 12px">支持单条或数组 { name, params }</span>
        </el-form-item>
        <el-form-item label="完整配置">
          <el-button plain data-testid="strategy-export-config" @click="exportStrategyConfig">导出完整配置</el-button>
          <el-button plain data-testid="strategy-import-config" @click="triggerImportConfig">导入配置</el-button>
          <input ref="configImportInput" type="file" accept=".json,application/json" style="display: none" data-testid="strategy-import-config-input" @change="handleImportConfig" />
          <span style="margin-left: 8px; color: #909399; font-size: 12px">下载/上传完整策略配置 JSON</span>
        </el-form-item>
        <el-form-item>
          <el-button
            type="primary"
            native-type="submit"
            :loading="saving"
            :disabled="loading || !isDirty"
            data-testid="strategy-save"
          >保存</el-button>
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
import { downloadCsv } from '../utils/csv'

interface StrategyForm {
  symbol: string
  market: 'US' | 'HK'
  buy_low: number
  sell_high: number
  short_selling: boolean
  min_profit_amount: number
  auto_resume_minutes: number
  max_daily_loss: number
  max_drawdown_amount: number | null
  max_consecutive_losses: number
  llm_interval_minutes: number
  fee_rate_us: number
  fee_rate_hk: number
  min_repricing_pct: number
  llm_action_cooldown_seconds: number
  trading_session_mode: 'ANY' | 'RTH_ONLY'
  margin_safety_factor: number
  allow_position_addons: boolean
  max_position_quantity: number
  max_position_notional: number
  max_risk_per_trade: number
  stop_loss_pct: number
  max_holding_minutes: number
  entry_cutoff_minutes_before_close: number
  flatten_minutes_before_close: number
  llm_order_execution_enabled: boolean
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
    max_drawdown_amount: null as number | null,
    max_consecutive_losses: 3,
    llm_interval_minutes: 2,
    fee_rate_us: 0.05,
    fee_rate_hk: 0.30,
    min_repricing_pct: 0.30,
    llm_action_cooldown_seconds: 60,
    margin_safety_factor: 0.9,
    allow_position_addons: false,
    max_position_quantity: 100,
    max_position_notional: 5000,
    max_risk_per_trade: 250,
    stop_loss_pct: 1,
    max_holding_minutes: 60,
    entry_cutoff_minutes_before_close: 45,
    flatten_minutes_before_close: 15,
    llm_order_execution_enabled: false,
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
      max_drawdown_amount: s.max_drawdown_amount,
      max_consecutive_losses: s.max_consecutive_losses,
      llm_interval_minutes: s.llm_interval_minutes,
      fee_rate_us: s.fee_rate_us * 100,
      fee_rate_hk: s.fee_rate_hk * 100,
      min_repricing_pct: s.min_repricing_pct * 100,
      llm_action_cooldown_seconds: s.llm_action_cooldown_seconds,
      margin_safety_factor: s.margin_safety_factor ?? 0.9,
      allow_position_addons: s.allow_position_addons ?? false,
      max_position_quantity: s.max_position_quantity ?? 100,
      max_position_notional: s.max_position_notional ?? 5000,
      max_risk_per_trade: s.max_risk_per_trade ?? 250,
      stop_loss_pct: s.stop_loss_pct ?? 1,
      max_holding_minutes: s.max_holding_minutes ?? 60,
      entry_cutoff_minutes_before_close: s.entry_cutoff_minutes_before_close ?? 45,
      flatten_minutes_before_close: s.flatten_minutes_before_close ?? 15,
      llm_order_execution_enabled: s.llm_order_execution_enabled ?? false,
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
    if (!previous || data.max_drawdown_amount !== previous.max_drawdown_amount) patch.max_drawdown_amount = data.max_drawdown_amount
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
    if (!previous || data.allow_position_addons !== previous.allow_position_addons) patch.allow_position_addons = data.allow_position_addons
    if (!previous || data.max_position_quantity !== previous.max_position_quantity) patch.max_position_quantity = data.max_position_quantity
    if (!previous || data.max_position_notional !== previous.max_position_notional) patch.max_position_notional = data.max_position_notional
    if (!previous || data.max_risk_per_trade !== previous.max_risk_per_trade) patch.max_risk_per_trade = data.max_risk_per_trade
    if (!previous || data.stop_loss_pct !== previous.stop_loss_pct) patch.stop_loss_pct = data.stop_loss_pct
    if (!previous || data.max_holding_minutes !== previous.max_holding_minutes) patch.max_holding_minutes = data.max_holding_minutes
    if (!previous || data.entry_cutoff_minutes_before_close !== previous.entry_cutoff_minutes_before_close) patch.entry_cutoff_minutes_before_close = data.entry_cutoff_minutes_before_close
    if (!previous || data.flatten_minutes_before_close !== previous.flatten_minutes_before_close) patch.flatten_minutes_before_close = data.flatten_minutes_before_close
    if (!previous || data.llm_order_execution_enabled !== previous.llm_order_execution_enabled) patch.llm_order_execution_enabled = data.llm_order_execution_enabled
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
const importInput = ref<HTMLInputElement | null>(null)
const configImportInput = ref<HTMLInputElement | null>(null)

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
    max_drawdown_amount: form.value.max_drawdown_amount,
    max_consecutive_losses: form.value.max_consecutive_losses,
    fee_rate_us: form.value.fee_rate_us / 100,
    fee_rate_hk: form.value.fee_rate_hk / 100,
    min_repricing_pct: form.value.min_repricing_pct / 100,
    llm_action_cooldown_seconds: form.value.llm_action_cooldown_seconds,
    trading_session_mode: form.value.trading_session_mode,
    margin_safety_factor: form.value.margin_safety_factor,
    allow_position_addons: form.value.allow_position_addons,
    max_position_quantity: form.value.max_position_quantity,
    max_position_notional: form.value.max_position_notional,
    max_risk_per_trade: form.value.max_risk_per_trade,
    stop_loss_pct: form.value.stop_loss_pct,
    max_holding_minutes: form.value.max_holding_minutes,
    entry_cutoff_minutes_before_close: form.value.entry_cutoff_minutes_before_close,
    flatten_minutes_before_close: form.value.flatten_minutes_before_close,
    llm_order_execution_enabled: form.value.llm_order_execution_enabled,
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

function downloadJson(filename: string, payload: unknown) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
}

function exportCurrentPreset() {
  const payload = {
    name: `${form.value.symbol || 'strategy'}-config`,
    params: buildPresetParams(),
  }
  downloadJson(`${payload.name}.json`, payload)
  ElMessage.success('已导出当前参数')
}

function triggerImport() {
  importInput.value?.click()
}

async function handleImport(evt: Event) {
  const target = evt.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return
  try {
    const text = await file.text()
    const parsed = JSON.parse(text)
    const list: Array<{ name: string; params: Record<string, unknown> }> = Array.isArray(parsed) ? parsed : [parsed]
    let created = 0
    for (const item of list) {
      if (!item.name || typeof item.name !== 'string' || !item.params || typeof item.params !== 'object') {
        ElMessage.warning('跳过格式不正确的条目')
        continue
      }
      await createStrategyPreset({ name: item.name.trim(), params: item.params as Record<string, unknown> })
      created += 1
    }
    await loadPresets()
    ElMessage.success(`成功导入 ${created} 条预设`)
  } catch {
    ElMessage.error('导入失败：请检查 JSON 格式')
  } finally {
    target.value = ''
  }
}

function buildStrategyConfig(): Record<string, unknown> {
  return {
    symbol: form.value.symbol,
    market: form.value.market,
    buy_low: form.value.buy_low,
    sell_high: form.value.sell_high,
    short_selling: form.value.short_selling,
    min_profit_amount: form.value.min_profit_amount,
    auto_resume_minutes: form.value.auto_resume_minutes,
    max_daily_loss: form.value.max_daily_loss,
    max_drawdown_amount: form.value.max_drawdown_amount,
    max_consecutive_losses: form.value.max_consecutive_losses,
    llm_interval_minutes: form.value.llm_interval_minutes,
    fee_rate_us: form.value.fee_rate_us / 100,
    fee_rate_hk: form.value.fee_rate_hk / 100,
    min_repricing_pct: form.value.min_repricing_pct / 100,
    llm_action_cooldown_seconds: form.value.llm_action_cooldown_seconds,
    trading_session_mode: form.value.trading_session_mode,
    margin_safety_factor: form.value.margin_safety_factor,
    allow_position_addons: form.value.allow_position_addons,
    max_position_quantity: form.value.max_position_quantity,
    max_position_notional: form.value.max_position_notional,
    max_risk_per_trade: form.value.max_risk_per_trade,
    stop_loss_pct: form.value.stop_loss_pct,
    max_holding_minutes: form.value.max_holding_minutes,
    entry_cutoff_minutes_before_close: form.value.entry_cutoff_minutes_before_close,
    flatten_minutes_before_close: form.value.flatten_minutes_before_close,
    llm_order_execution_enabled: form.value.llm_order_execution_enabled,
    report_schedule_enabled: form.value.report_schedule_enabled,
    report_schedule_interval_hours: form.value.report_schedule_interval_hours,
    report_schedule_symbol: form.value.report_schedule_symbol,
  }
}

function exportStrategyConfig() {
  const payload = buildStrategyConfig()
  downloadJson(`strategy-config-${form.value.symbol || 'default'}.json`, payload)
  ElMessage.success('已导出完整配置')
}

function triggerImportConfig() {
  configImportInput.value?.click()
}

async function handleImportConfig(evt: Event) {
  const target = evt.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return
  try {
    const text = await file.text()
    const parsed = JSON.parse(text)
    if (!parsed || typeof parsed !== 'object') {
      ElMessage.error('导入失败：配置格式不正确')
      return
    }
    const numeric = (v: unknown): number | undefined => {
      if (v === undefined) return undefined
      const n = Number(v)
      return Number.isNaN(n) ? undefined : n
    }
    const bool = (v: unknown): boolean | undefined => {
      if (typeof v === 'boolean') return v
      return undefined
    }
    const str = (v: unknown): string | undefined => (typeof v === 'string' ? v : undefined)
    const patch: Partial<StrategyForm> = {}
    if (str(parsed.symbol) !== undefined) patch.symbol = str(parsed.symbol)
    if (str(parsed.market) !== undefined) patch.market = parsed.market as 'US' | 'HK'
    if (numeric(parsed.buy_low) !== undefined) patch.buy_low = numeric(parsed.buy_low)!
    if (numeric(parsed.sell_high) !== undefined) patch.sell_high = numeric(parsed.sell_high)!
    if (bool(parsed.short_selling) !== undefined) patch.short_selling = bool(parsed.short_selling)!
    if (numeric(parsed.min_profit_amount) !== undefined) patch.min_profit_amount = numeric(parsed.min_profit_amount)!
    if (numeric(parsed.auto_resume_minutes) !== undefined) patch.auto_resume_minutes = numeric(parsed.auto_resume_minutes)!
    if (numeric(parsed.max_daily_loss) !== undefined) patch.max_daily_loss = numeric(parsed.max_daily_loss)!
    if (parsed.max_drawdown_amount === null) patch.max_drawdown_amount = null
    else if (numeric(parsed.max_drawdown_amount) !== undefined) patch.max_drawdown_amount = numeric(parsed.max_drawdown_amount)!
    if (numeric(parsed.max_consecutive_losses) !== undefined) patch.max_consecutive_losses = numeric(parsed.max_consecutive_losses)!
    if (numeric(parsed.llm_interval_minutes) !== undefined) patch.llm_interval_minutes = numeric(parsed.llm_interval_minutes)!
    if (numeric(parsed.fee_rate_us) !== undefined) patch.fee_rate_us = numeric(parsed.fee_rate_us)! * 100
    if (numeric(parsed.fee_rate_hk) !== undefined) patch.fee_rate_hk = numeric(parsed.fee_rate_hk)! * 100
    if (numeric(parsed.min_repricing_pct) !== undefined) patch.min_repricing_pct = numeric(parsed.min_repricing_pct)! * 100
    if (numeric(parsed.llm_action_cooldown_seconds) !== undefined) patch.llm_action_cooldown_seconds = numeric(parsed.llm_action_cooldown_seconds)!
    if (str(parsed.trading_session_mode) !== undefined) patch.trading_session_mode = parsed.trading_session_mode as 'ANY' | 'RTH_ONLY'
    if (numeric(parsed.margin_safety_factor) !== undefined) patch.margin_safety_factor = numeric(parsed.margin_safety_factor)!
    if (bool(parsed.allow_position_addons) !== undefined) patch.allow_position_addons = bool(parsed.allow_position_addons)!
    if (numeric(parsed.max_position_quantity) !== undefined) patch.max_position_quantity = numeric(parsed.max_position_quantity)!
    if (numeric(parsed.max_position_notional) !== undefined) patch.max_position_notional = numeric(parsed.max_position_notional)!
    if (numeric(parsed.max_risk_per_trade) !== undefined) patch.max_risk_per_trade = numeric(parsed.max_risk_per_trade)!
    if (numeric(parsed.stop_loss_pct) !== undefined) patch.stop_loss_pct = numeric(parsed.stop_loss_pct)!
    if (numeric(parsed.max_holding_minutes) !== undefined) patch.max_holding_minutes = numeric(parsed.max_holding_minutes)!
    if (numeric(parsed.entry_cutoff_minutes_before_close) !== undefined) patch.entry_cutoff_minutes_before_close = numeric(parsed.entry_cutoff_minutes_before_close)!
    if (numeric(parsed.flatten_minutes_before_close) !== undefined) patch.flatten_minutes_before_close = numeric(parsed.flatten_minutes_before_close)!
    if (bool(parsed.llm_order_execution_enabled) !== undefined) patch.llm_order_execution_enabled = bool(parsed.llm_order_execution_enabled)!
    if (bool(parsed.report_schedule_enabled) !== undefined) patch.report_schedule_enabled = bool(parsed.report_schedule_enabled)!
    if (numeric(parsed.report_schedule_interval_hours) !== undefined) patch.report_schedule_interval_hours = numeric(parsed.report_schedule_interval_hours)!
    if (str(parsed.report_schedule_symbol) !== undefined) patch.report_schedule_symbol = str(parsed.report_schedule_symbol)

    form.value = { ...form.value, ...patch }
    ElMessage.success('配置已导入，请确认后保存')
  } catch {
    ElMessage.error('导入失败：请检查 JSON 格式')
  } finally {
    target.value = ''
  }
}

const llmStatus = ref<LLMIntervalStatus>({
  enabled: false,
  shadow_mode: true,
  policy_status: 'SHADOW',
  interval_minutes: 2,
  last_analysis_at: null,
  next_analysis_at: null,
  current_suggestion: null,
  applied_values: null,
  last_applied_values: null,
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

/** Derived consistency between the LLM suggestion, the last applied values, and
 * the live form. Surfaces "建议未应用" / "配置已偏离建议" so the user can spot drift
 * without comparing numbers by eye. Reuses only already-loaded fields. */
const llmConsistencyHint = computed<{ text: string; type: 'warning' | 'info' | 'success' } | null>(() => {
  const sug = llmStatus.value.current_suggestion
  const applied = llmStatus.value.applied_values
  if (!sug) return null
  const sugMatchesApplied = applied && applied.buy_low === sug.buy_low && applied.sell_high === sug.sell_high
  if (applied && !sugMatchesApplied) {
    return { text: '建议未应用：最新建议与已应用值不一致', type: 'warning' }
  }
  if (applied && (applied.buy_low !== form.value.buy_low || applied.sell_high !== form.value.sell_high)) {
    return { text: '配置已偏离已应用区间', type: 'info' }
  }
  if (applied && sugMatchesApplied) {
    return { text: '建议已应用且与当前配置一致', type: 'success' }
  }
  return { text: '有新建议待应用', type: 'warning' }
})

const llmInteractionSuccessCount = computed(() => llmInteractions.value.filter((i) => i.success).length)
const llmInteractionSuccessRate = computed(() => {
  if (llmInteractions.value.length === 0) return 0
  return (llmInteractionSuccessCount.value / llmInteractions.value.length) * 100
})

/** Range gross-margin readout: spread minus an estimated round-trip fee using the
 * configured per-side fee rate for the current market. Purely advisory — the
 * real fee gate lives in TradeExecutionService; this just previews the math. */
const rangeReadout = computed<{ spread: number; estRoundTripFee: number; net: number } | null>(() => {
  const bl = form.value.buy_low
  const sh = form.value.sell_high
  if (!(bl > 0) || !(sh > bl)) return null
  const spread = sh - bl
  const sideRate = (form.value.market === 'HK' ? form.value.fee_rate_hk : form.value.fee_rate_us) / 100
  // Two sides (buy + sell), each rate * notional; notional approximated by the
  // midpoint price so the estimate is order-agnostic.
  const mid = (bl + sh) / 2
  const estRoundTripFee = sideRate * mid * 2
  return { spread, estRoundTripFee, net: spread - estRoundTripFee }
})

function exportLLMInteractions() {
  const rows = llmInteractions.value.map((r) => ({
    created_at: r.created_at,
    success: r.success ? 'yes' : 'no',
    order_action: r.order_action ?? '',
    order_status: r.order_status ?? '',
    applied: r.applied ? 'yes' : 'no',
    error: r.error ?? '',
  }))
  downloadCsv('llm_interactions.csv', [
    { key: 'created_at', label: 'created_at' },
    { key: 'success', label: 'success' },
    { key: 'order_action', label: 'order_action' },
    { key: 'order_status', label: 'order_status' },
    { key: 'applied', label: 'applied' },
    { key: 'error', label: 'error' },
  ], rows)
  ElMessage.success(`已导出 ${rows.length} 条 LLM 交互`)
}

const analyzing = ref(false)

const previewSymbol = ref('')
const previewMarket = ref<'US' | 'HK'>('US')
const previewShortSelling = ref(false)
const previewing = ref(false)
const previewResult = ref<LLMAnalyzeResponse | null>(null)
const previewError = ref<string | null>(null)

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
.range-readout {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 13px;
  color: #606266;
  line-height: 32px;
}

.range-readout .positive {
  color: #14884f;
}

.range-readout .negative {
  color: #c43838;
}

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
