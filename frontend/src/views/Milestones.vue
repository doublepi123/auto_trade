<script setup lang="ts">
import { ref } from 'vue'
import { getMilestones, type MilestoneResult } from '../api/milestones'
import StatisticsQualityAlert from '../components/StatisticsQualityAlert.vue'

const loading = ref(false)
const result = ref<MilestoneResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(365)
const step = ref(100)

async function run() {
  loading.value = true
  try {
    result.value = await getMilestones({
      symbol: symbol.value || undefined,
      lookback_days: lookbackDays.value,
      step: step.value,
    })
  } finally {
    loading.value = false
  }
}

const accelLabel: Record<string, string> = {
  accelerating: '加速',
  decelerating: '减速',
  stable: '稳定',
  insufficient: '样本不足',
}
</script>

<template>
  <div class="page-container">
    <h2>PnL 里程碑</h2>
    <p class="page-desc">追踪累计 PnL 里程碑的达成节奏与加速度（灵感来自 Edgewonk / QuantStats）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="symbol" placeholder="全部" clearable style="width: 140px" />
        </el-form-item>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="7" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item label="步长">
          <el-input-number v-model="step" :min="10" :max="10000" :step="50" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">追踪</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <template v-if="result">
      <StatisticsQualityAlert :quality="result.statistics_quality" />
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="最终累计 PnL" :value="result.final_cumulative_pnl" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="上行里程碑" :value="result.up_milestones" />
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="下行里程碑" :value="result.down_milestones" />
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">上行节奏</span>
                <span class="stat-value">{{ result.pace.avg_up_pace ?? '-' }} 笔/里程碑</span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="4">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">加速度</span>
                <el-tag size="small">{{ accelLabel[result.pace.up_acceleration] ?? result.pace.up_acceleration }}</el-tag>
              </div>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>里程碑时间线（最近 20 个）</template>
          <el-table :data="result.milestones.slice().reverse()" size="small" max-height="400">
            <el-table-column prop="level" label="里程碑" width="100" />
            <el-table-column prop="trade_index" label="第 N 笔" width="80" />
            <el-table-column prop="direction" label="方向" width="80">
              <template #default="{ row }">
                <el-tag :type="row.direction === 'up' ? 'success' : 'danger'" size="small">
                  {{ row.direction === 'up' ? '↑' : '↓' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-card>
      </template>
    </template>
  </div>
</template>

<style scoped>
.page-container { padding: 20px; }
.page-desc { color: #909399; margin-bottom: 16px; }
.control-card { margin-bottom: 8px; }
.stat-custom { display: flex; flex-direction: column; align-items: center; gap: 4px; }
.stat-label { font-size: 12px; color: #909399; }
.stat-value { font-size: 16px; font-weight: 600; }
</style>
