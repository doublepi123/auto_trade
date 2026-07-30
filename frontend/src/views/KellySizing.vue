<script setup lang="ts">
import { ref } from 'vue'
import { getKellySizing, type KellyResult } from '../api/kelly'

const loading = ref(false)
const result = ref<KellyResult | null>(null)
const symbol = ref('')
const lookbackDays = ref(90)

async function run() {
  loading.value = true
  try {
    result.value = await getKellySizing({
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
    <h2>Kelly 仓位定尺</h2>
    <p class="page-desc">基于历史胜率与盈亏比的 Kelly 公式最优仓位比例（灵感来自 Freqtrade / QuantConnect）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="标的">
          <el-input v-model="symbol" placeholder="全部" clearable style="width: 140px" />
        </el-form-item>
        <el-form-item label="回看天数">
          <el-input-number v-model="lookbackDays" :min="7" :max="3650" :step="30" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="loading" @click="run">计算</el-button>
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
              <el-statistic title="盈亏比" :value="result.payoff_ratio" :precision="2" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="Full Kelly" :value="result.kelly_full_pct" :precision="2" suffix="%" />
            </el-card>
          </el-col>
          <el-col :span="6">
            <el-card shadow="hover">
              <el-statistic title="样本数" :value="result.sample_size" />
            </el-card>
          </el-col>
        </el-row>

        <el-alert
          :title="result.recommendation"
          type="info"
          :closable="false"
          style="margin-top: 16px"
        />

        <el-card style="margin-top: 16px">
          <template #header>Kelly 变体</template>
          <el-table :data="result.variants" size="small">
            <el-table-column prop="label" label="变体" width="140" />
            <el-table-column prop="allocation_pct" label="配置比例 (%)" width="140">
              <template #default="{ row }">
                <el-progress
                  :percentage="Math.min(row.allocation_pct, 100)"
                  :stroke-width="14"
                  :format="() => row.allocation_pct + '%'"
                />
              </template>
            </el-table-column>
            <el-table-column prop="expected_growth" label="期望对数增长率" width="160">
              <template #default="{ row }">
                {{ row.expected_growth.toFixed(6) }}
              </template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card style="margin-top: 16px">
          <template #header>样本统计</template>
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="平均盈利">{{ result.avg_win }}</el-descriptions-item>
            <el-descriptions-item label="平均亏损">{{ result.avg_loss }}</el-descriptions-item>
            <el-descriptions-item label="回看天数">{{ result.lookback_days }}</el-descriptions-item>
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
