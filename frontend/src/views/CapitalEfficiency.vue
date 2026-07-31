<script setup lang="ts">
import { ref } from 'vue'
import { getCapitalEfficiency, type CapitalEfficiencyResult } from '../api/capitalEfficiency'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'

const loading = ref(false)
const result = ref<CapitalEfficiencyResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(180)
const capitalBase = ref(10000)

async function run() {
  loading.value = true
  try {
    result.value = await getCapitalEfficiency({
      symbol: symbol.value || undefined,
      lookback_days: lookbackDays.value,
      capital_base: capitalBase.value,
    })
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="page-container">
    <h2>闭环资金效率</h2>
    <p class="page-desc">基于已完成 FIFO 闭环交易的资本回报、换手与资本时间利用率；当前未平仓仓位不在证据范围内</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="symbol" placeholder="全部" clearable style="width: 140px" />
        </el-form-item>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="7" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item label="资金基数">
          <el-input-number v-model="capitalBase" :min="100" :max="10000000" :step="1000" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">分析</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="result">
      <StatisticsQualityAlert :quality="result.statistics_quality" />
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-alert :title="result.assessment" type="info" :closable="false" style="margin-top: 16px" />
        <el-alert
          title="证据范围仅包含回看窗口内完成平仓的闭环交易，不包含当前未平仓仓位。"
          type="warning"
          :closable="false"
          style="margin-top: 8px"
        />

        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="资本回报率" :value="result.return_on_capital" :precision="2" suffix="%" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="年化 ROC" :value="result.annualized_roc" :precision="2" suffix="%" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="换手率" :value="result.turnover_ratio" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic
                title="闭环资本时间利用率"
                :value="result.capital_time_utilization_rate * 100"
                :precision="2"
                suffix="%"
              />
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>效率详情</template>
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="总 PnL">{{ result.total_pnl }} {{ result.currency ?? '' }}</el-descriptions-item>
            <el-descriptions-item label="资金基数">{{ result.capital_base }} {{ result.capital_base_currency ?? '' }}</el-descriptions-item>
            <el-descriptions-item label="平均闭环占用资金">
              {{ result.average_closed_round_trip_capital }} {{ result.currency ?? '' }}
            </el-descriptions-item>
            <el-descriptions-item label="单位交易收益">{{ result.pnl_per_unit_traded }}</el-descriptions-item>
            <el-descriptions-item label="盈利交易入场名义本金占比">
              {{ (result.winning_entry_notional_share * 100).toFixed(1) }}%
            </el-descriptions-item>
            <el-descriptions-item label="平仓活跃日率">
              {{ (result.exit_active_day_rate * 100).toFixed(1) }}%（{{ result.exit_active_days }} 天）
            </el-descriptions-item>
            <el-descriptions-item label="入场名义本金合计">
              {{ result.total_entry_notional }} {{ result.currency ?? '' }}
            </el-descriptions-item>
            <el-descriptions-item label="样本数">{{ result.sample_size }}</el-descriptions-item>
          </el-descriptions>
        </el-card>
      </template>
    </template>
  </div>
</template>

<style scoped>
.page-container { padding: 20px; }
.page-desc { color: #909399; margin-bottom: 16px; }
.control-card { margin-bottom: 8px; }
</style>
