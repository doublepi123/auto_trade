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
        <p>建议区间: {{ llmStatus.current_suggestion.buy_low.toFixed(2) }} ~ {{ llmStatus.current_suggestion.sell_high.toFixed(2) }}</p>
        <p>分析: {{ llmStatus.current_suggestion.analysis }}</p>
      </div>
      <div v-if="llmStatus.applied_values" style="margin-top: 8px">
        <p>已应用: {{ llmStatus.applied_values.buy_low.toFixed(2) }} ~ {{ llmStatus.applied_values.sell_high.toFixed(2) }}</p>
      </div>
      <div v-if="llmStatus.reject_reason" style="margin-top: 8px; color: #f56c6c">
        <p>上次被拒: {{ llmStatus.reject_reason }}</p>
      </div>
      <div style="margin-top: 12px">
        <p style="margin-bottom: 8px">
          刷新间隔：{{ llmStatus.interval_minutes }} 分钟
        </p>
        <el-button size="small" :loading="analyzing" @click="triggerAnalyze">
          立即重新分析
        </el-button>
        <span v-if="llmStatus.next_analysis_at" style="margin-left: 12px; color: #909399; font-size: 12px">
          下次分析: {{ formatTime(llmStatus.next_analysis_at) }}
        </span>
      </div>
    </el-card>

    <el-card style="max-width: 600px">
      <el-form :model="form" label-width="180px" @submit.prevent="save">
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
          <el-input-number v-model="form.buy_low" :min="0.01" :precision="2" />
        </el-form-item>
        <el-form-item label="卖出价上限">
          <el-input-number v-model="form.sell_high" :min="0.01" :precision="2" />
        </el-form-item>
        <el-form-item label="做空">
          <el-switch v-model="form.short_selling" />
        </el-form-item>
        <el-form-item label="单日最大亏损">
          <el-input-number v-model="form.max_daily_loss" :min="1" :precision="2" />
        </el-form-item>
        <el-form-item label="连续亏损暂停阈值">
          <el-input-number v-model="form.max_consecutive_losses" :min="1" />
        </el-form-item>
        <el-form-item label="LLM刷新间隔（分钟）">
          <el-input-number v-model="form.llm_interval_minutes" :min="15" :max="1440" :step="15" />
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
import { ref, onMounted } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getStrategy, updateStrategy, getLLMIntervalStatus, analyzeLLMInterval, enableLLMInterval, disableLLMInterval } from '../api'
import { useFormState } from '../composables/useFormState'
import type { LLMIntervalStatus } from '../types'

const { form, loading, saving, saved, error, isDirty, load, save } = useFormState({
  initial: {
    symbol: '',
    market: 'US' as 'US' | 'HK',
    buy_low: 0,
    sell_high: 0,
    short_selling: false,
    max_daily_loss: 5000,
    max_consecutive_losses: 3,
    llm_interval_minutes: 240,
  },
  load: async () => {
    const s = await getStrategy()
    return {
      symbol: s.symbol,
      market: s.market,
      buy_low: s.buy_low,
      sell_high: s.sell_high,
      short_selling: s.short_selling,
      max_daily_loss: s.max_daily_loss,
      max_consecutive_losses: s.max_consecutive_losses,
      llm_interval_minutes: s.llm_interval_minutes,
    }
  },
  save: async (data) => {
    await updateStrategy(data)
  },
})

const llmStatus = ref<LLMIntervalStatus>({
  enabled: false,
  interval_minutes: 240,
  last_analysis_at: null,
  next_analysis_at: null,
  current_suggestion: null,
  applied_values: null,
  reject_reason: null,
})

const analyzing = ref(false)

const loadLLMStatus = async () => {
  try {
    llmStatus.value = await getLLMIntervalStatus()
  } catch {
    // silent
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
  } catch {
    ElMessage.error('分析失败')
  } finally {
    analyzing.value = false
  }
}

const formatTime = (iso: string | null) => {
  if (!iso) return '-'
  return new Date(iso).toLocaleString('zh-CN')
}

load()
loadLLMStatus()

onBeforeRouteLeave(() => {
  if (!isDirty.value) return true
  return ElMessageBox.confirm('策略配置尚未保存，确定要离开当前页面吗？', '未保存的更改', { type: 'warning' })
    .then(() => true)
    .catch(() => false)
})
</script>
