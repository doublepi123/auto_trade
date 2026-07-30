<script setup lang="ts">
import { ref } from 'vue'
import { getStreakAnalysis, type StreakResult } from '../api/streaks'

const loading = ref(false)
const result = ref<StreakResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(180)

async function run() {
  loading.value = true
  try {
    result.value = await getStreakAnalysis({
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
    <h2>连胜连败分析</h2>
    <p class="page-desc">胜负连续分布、当前连续与概率估算（灵感来自 QuantStats）</p>

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
      <el-alert
        v-if="result.error"
        :title="result.error"
        type="warning"
        :closable="false"
        style="margin-top: 16px"
      />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="胜率" :value="result.win_rate * 100" :precision="1" suffix="%" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <div class="stat-custom">
                <span class="stat-label">当前连续</span>
                <span class="stat-value" :class="result.current_streak.type === 'win' ? 'text-green' : 'text-red'">
                  {{ result.current_streak.length }} 连{{ result.current_streak.type === 'win' ? '胜' : '败' }}
                </span>
              </div>
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="最长连胜" :value="result.win_streaks.max" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="最长连败" :value="result.loss_streaks.max" />
            </el-card>
          </el-col>
        </el-row>

        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="12">
            <el-card>
              <template #header>连胜分布</template>
              <el-table :data="result.win_streaks.distribution" size="small">
                <el-table-column prop="length" label="连续长度" width="100" />
                <el-table-column prop="count" label="出现次数" width="100" />
                <el-table-column label="柱状">
                  <template #default="{ row }">
                    <el-progress :percentage="Math.min(row.count * 10, 100)" :show-text="false" status="success" :stroke-width="12" />
                  </template>
                </el-table-column>
              </el-table>
            </el-card>
          </el-col>
          <el-col :span="12">
            <el-card>
              <template #header>连败分布</template>
              <el-table :data="result.loss_streaks.distribution" size="small">
                <el-table-column prop="length" label="连续长度" width="100" />
                <el-table-column prop="count" label="出现次数" width="100" />
                <el-table-column label="柱状">
                  <template #default="{ row }">
                    <el-progress :percentage="Math.min(row.count * 10, 100)" :show-text="false" status="exception" :stroke-width="12" />
                  </template>
                </el-table-column>
              </el-table>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>概率估算（基于经验胜率）</template>
          <el-descriptions :column="4" border size="small">
            <el-descriptions-item label="3连胜概率">{{ (result.probability.win_streak_3 * 100).toFixed(2) }}%</el-descriptions-item>
            <el-descriptions-item label="5连胜概率">{{ (result.probability.win_streak_5 * 100).toFixed(2) }}%</el-descriptions-item>
            <el-descriptions-item label="3连败概率">{{ (result.probability.loss_streak_3 * 100).toFixed(2) }}%</el-descriptions-item>
            <el-descriptions-item label="5连败概率">{{ (result.probability.loss_streak_5 * 100).toFixed(2) }}%</el-descriptions-item>
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
.stat-custom { display: flex; flex-direction: column; }
.stat-label { font-size: 12px; color: #909399; }
.stat-value { font-size: 24px; font-weight: 600; margin-top: 4px; }
.text-green { color: #67c23a; }
.text-red { color: #f56c6c; }
</style>
