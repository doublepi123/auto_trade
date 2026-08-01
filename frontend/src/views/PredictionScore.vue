<script setup lang="ts">
import { ref } from 'vue'
import { getPredictionScore, type PredictionScoreResult } from '../api/predictionScore'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'

const loading = ref(false)
const result = ref<PredictionScoreResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(180)

async function run() {
  loading.value = true
  try {
    result.value = await getPredictionScore({
      symbol: symbol.value || undefined,
      lookback_days: lookbackDays.value,
    })
  } finally {
    loading.value = false
  }
}

const dowNames = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

function wrColor(v: number, baseline: number): string {
  if (v > baseline + 0.05) return '#67c23a'
  if (v < baseline - 0.05) return '#f56c6c'
  return '#909399'
}
</script>

<template>
  <div class="page-container">
    <h2>条件胜率评分</h2>
    <p class="page-desc">按入场时可观测的星期、时段和已知连续状态统计条件胜率；仅为回顾性频率，不可直接作为实时交易信号</p>

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
      <StatisticsQualityAlert :quality="result.statistics_quality" style="margin-top: 16px" />
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="基线胜率" :value="result.baseline_win_rate * 100" :precision="1" suffix="%" />
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="优势价差" :value="result.edge_spread * 100" :precision="1" suffix="%" />
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="样本数" :value="result.sample_size" />
            </el-card>
          </el-col>
        </el-row>

        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="12">
            <el-card>
              <template #header>Top 条件优势</template>
              <el-table :data="result.top_edges" size="small">
                <el-table-column prop="feature" label="特征" width="120" />
                <el-table-column prop="win_rate" label="胜率" width="100">
                  <template #default="{ row }">
                    <span :style="{ color: wrColor(row.win_rate, result.baseline_win_rate) }">
                      {{ (row.win_rate * 100).toFixed(1) }}%
                    </span>
                  </template>
                </el-table-column>
                <el-table-column prop="count" label="样本" width="60" />
              </el-table>
            </el-card>
          </el-col>
          <el-col :span="12">
            <el-card>
              <template #header>Bottom 条件劣势</template>
              <el-table :data="result.bottom_edges" size="small">
                <el-table-column prop="feature" label="特征" width="120" />
                <el-table-column prop="win_rate" label="胜率" width="100">
                  <template #default="{ row }">
                    <span :style="{ color: wrColor(row.win_rate, result.baseline_win_rate) }">
                      {{ (row.win_rate * 100).toFixed(1) }}%
                    </span>
                  </template>
                </el-table-column>
                <el-table-column prop="count" label="样本" width="60" />
              </el-table>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>按星期胜率</template>
          <el-descriptions :column="7" border size="small">
            <el-descriptions-item v-for="(wr, d) in result.dow_win_rates" :key="d" :label="dowNames[Number(d)] ?? d">
              <span :style="{ color: wrColor(wr, result.baseline_win_rate) }">{{ (wr * 100).toFixed(0) }}%</span>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>连续状态胜率</template>
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item v-for="(wr, k) in result.streak_win_rates" :key="k" :label="k === 'after_win' ? '赢后' : k === 'after_loss' ? '亏后' : '中性'">
              {{ (wr * 100).toFixed(1) }}%
            </el-descriptions-item>
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
