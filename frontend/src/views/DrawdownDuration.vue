<script setup lang="ts">
import { ref } from 'vue'
import { getDrawdownDuration, type DrawdownDurationResult } from '../api/drawdownDuration'

const loading = ref(false)
const result = ref<DrawdownDurationResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(365)

async function run() {
  loading.value = true
  try {
    result.value = await getDrawdownDuration({
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
    <h2>回撤持续期</h2>
    <p class="page-desc">回撤 episode 的持续时长分布，评估恢复速度（灵感来自 QuantStats / VectorBT）</p>

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
      <el-alert v-if="result.error" :title="result.error" type="warning" :closable="false" style="margin-top: 16px" />
      <el-alert v-else-if="result.note" :title="result.note" type="info" :closable="false" style="margin-top: 16px" />

      <template v-else>
        <el-row :gutter="16" style="margin-top: 16px">
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="回撤 episodes" :value="result.episodes" />
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="平均持续(笔)" :value="result.summary.avg" :precision="1" />
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="最长持续(笔)" :value="result.summary.max" />
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="中位持续(笔)" :value="result.summary.median" />
            </el-card>
          </el-col>
          <el-col :span="4">
            <el-card shadow="hover">
              <el-statistic title="水下时间%" :value="result.pct_time_underwater" :precision="1" suffix="%" />
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>持续期分布</template>
          <el-table :data="result.histogram" size="small">
            <el-table-column prop="duration" label="持续笔数" width="100" />
            <el-table-column prop="count" label="出现次数" width="100" />
            <el-table-column label="分布">
              <template #default="{ row }">
                <el-progress :percentage="Math.min(row.count * 15, 100)" :show-text="false" :stroke-width="12" status="exception" />
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>分位数</template>
          <el-descriptions :column="4" border size="small">
            <el-descriptions-item label="P25">{{ result.summary.p25 }}</el-descriptions-item>
            <el-descriptions-item label="中位">{{ result.summary.median }}</el-descriptions-item>
            <el-descriptions-item label="P75">{{ result.summary.p75 }}</el-descriptions-item>
            <el-descriptions-item label="最大">{{ result.summary.max }}</el-descriptions-item>
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
