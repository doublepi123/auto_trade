<script setup lang="ts">
import { ref } from 'vue'
import { getAsymmetry, type AsymmetryResult } from '../api/asymmetry'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'

const loading = ref(false)
const result = ref<AsymmetryResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(180)

async function run() {
  loading.value = true
  try {
    result.value = await getAsymmetry({
      symbol: symbol.value || undefined,
      lookback_days: lookbackDays.value,
    })
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="page-container">
    <h2>胜负不对称性</h2>
    <p class="page-desc">盈利与亏损交易的分布不对称分析：幅度、集中度与条件模式（灵感来自 QuantStats / Edgewonk）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="symbol" placeholder="全部" clearable style="width: 140px" />
        </el-form-item>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="7" :max="3650" :step="30" />
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

        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic
                v-if="result.asymmetry_ratio !== null"
                title="不对称比"
                :value="result.asymmetry_ratio"
                :precision="2"
              />
              <div v-else class="undefined-stat">
                <span class="undefined-label">不对称比</span>
                <span class="undefined-value">未定义</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="总盈利" :value="result.total_win" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="总亏损" :value="result.total_loss" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="净优势" :value="result.net_edge" :precision="2" />
            </el-card>
          </el-col>
        </el-row>

        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="12">
            <el-card>
              <template #header>盈利交易</template>
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="笔数">{{ result.win_stats.count }}</el-descriptions-item>
                <el-descriptions-item label="总额">{{ result.win_stats.total }}</el-descriptions-item>
                <el-descriptions-item label="平均">{{ result.win_stats.avg }}</el-descriptions-item>
                <el-descriptions-item label="中位">{{ result.win_stats.median }}</el-descriptions-item>
                <el-descriptions-item label="最大盈利">{{ result.win_stats.largest_magnitude }}</el-descriptions-item>
                <el-descriptions-item label="Top3 占比">{{ (result.win_stats.top3_share * 100).toFixed(1) }}%</el-descriptions-item>
              </el-descriptions>
            </el-card>
          </el-col>
          <el-col :span="12">
            <el-card>
              <template #header>亏损交易</template>
              <el-descriptions :column="2" border size="small">
                <el-descriptions-item label="笔数">{{ result.loss_stats.count }}</el-descriptions-item>
                <el-descriptions-item label="总额">{{ result.loss_stats.total }}</el-descriptions-item>
                <el-descriptions-item label="平均">{{ result.loss_stats.avg }}</el-descriptions-item>
                <el-descriptions-item label="中位">{{ result.loss_stats.median }}</el-descriptions-item>
                <el-descriptions-item label="最大亏损（净 PnL）">{{ result.loss_stats.largest_magnitude }}</el-descriptions-item>
                <el-descriptions-item label="Top3 占比">{{ (result.loss_stats.top3_share * 100).toFixed(1) }}%</el-descriptions-item>
              </el-descriptions>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>条件模式</template>
          <el-descriptions :column="2" border size="small">
            <el-descriptions-item label="大赢后下一笔均值">{{ result.conditional.after_big_win_avg ?? '-' }}</el-descriptions-item>
            <el-descriptions-item label="大赢后样本数">{{ result.conditional.after_big_win_count }}</el-descriptions-item>
            <el-descriptions-item label="大亏后下一笔均值">{{ result.conditional.after_big_loss_avg ?? '-' }}</el-descriptions-item>
            <el-descriptions-item label="大亏后样本数">{{ result.conditional.after_big_loss_count }}</el-descriptions-item>
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
.undefined-stat { display: flex; min-height: 58px; flex-direction: column; align-items: center; justify-content: center; }
.undefined-label { color: #909399; font-size: 13px; }
.undefined-value { margin-top: 8px; color: #909399; font-size: 20px; font-weight: 600; }
</style>
