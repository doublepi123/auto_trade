<script setup lang="ts">
import { ref } from 'vue'
import { getRecoveryTimeline, type RecoveryResult } from '../api/recovery'

const loading = ref(false)
const result = ref<RecoveryResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(365)

async function run() {
  loading.value = true
  try {
    result.value = await getRecoveryTimeline({
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
    <h2>回撤恢复时间线</h2>
    <p class="page-desc">识别每段回撤 episode 并度量恢复所需交易数（灵感来自 QuantStats 水下曲线）</p>

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
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="回撤 episodes" :value="result.total_episodes" />
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="已恢复" :value="result.recovered_count" />
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="仍水下" :value="result.underwater_count" />
            </el-card>
          </el-col>
          <el-col :span="5">
            <el-card shadow="hover">
              <el-statistic title="平均恢复(笔)" :value="result.avg_recovery_trades ?? 0" :precision="1" />
            </el-card>
          </el-col>
          <el-col :span="4">
            <el-card shadow="hover">
              <el-statistic title="最大回撤" :value="result.max_drawdown" :precision="2" />
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>回撤 Episodes（最近 20 段）</template>
          <el-table :data="result.episodes.slice().reverse()" size="small" max-height="500">
            <el-table-column prop="peak_trade_index" label="峰值#" width="70" />
            <el-table-column prop="trough_trade_index" label="谷值#" width="70" />
            <el-table-column prop="drawdown" label="回撤额" width="100">
              <template #default="{ row }">
                <span class="text-red">{{ row.drawdown }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="drawdown_pct" label="回撤%" width="90">
              <template #default="{ row }">{{ row.drawdown_pct }}%</template>
            </el-table-column>
            <el-table-column prop="duration_trades" label="下跌笔数" width="90" />
            <el-table-column label="恢复笔数" width="90">
              <template #default="{ row }">
                <span v-if="row.recovered">{{ row.recovery_trades }}</span>
                <el-tag v-else type="danger" size="small">水下</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="80">
              <template #default="{ row }">
                <el-tag :type="row.recovered ? 'success' : 'danger'" size="small">
                  {{ row.recovered ? '已恢复' : '未恢复' }}
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
.text-red { color: #f56c6c; }
</style>
