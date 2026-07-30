<script setup lang="ts">
import { ref } from 'vue'
import { getTagAnalytics, type TagAnalyticsResult } from '../api/tagAnalytics'

const loading = ref(false)
const result = ref<TagAnalyticsResult | null>(null)
const minTrades = ref(2)

async function run() {
  loading.value = true
  try {
    result.value = await getTagAnalytics({ min_trades: minTrades.value })
  } finally {
    loading.value = false
  }
}

function pnlColor(v: number): string {
  return v > 0 ? '#67c23a' : v < 0 ? '#f56c6c' : '#909399'
}
</script>

<template>
  <div class="page-container">
    <h2>标签绩效</h2>
    <p class="page-desc">按交易笔记标签聚合绩效，发现哪些定性标签与更好/更差的结果相关（灵感来自 Freqtrade / Edgewonk）</p>

    <el-card class="control-card">
      <el-form inline>
        <el-form-item label="最少交易数">
          <el-input-number v-model="minTrades" :min="1" :max="50" />
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
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="笔记总数" :value="result.total_notes" />
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="唯一标签" :value="result.unique_tags" />
            </el-card>
          </el-col>
          <el-col :span="8">
            <el-card shadow="hover">
              <el-statistic title="达标标签" :value="result.qualifying_tags" />
            </el-card>
          </el-col>
        </el-row>

        <el-row :gutter="16" style="margin-top: 16px" v-if="result.best_tag">
          <el-col :span="12">
            <el-card shadow="hover" class="highlight-card">
              <span class="hl-label">最佳标签</span>
              <el-tag type="success" size="large">{{ result.best_tag.tag }}</el-tag>
              <span class="hl-pnl" :style="{ color: pnlColor(result.best_tag.total_pnl) }">
                {{ result.best_tag.total_pnl }} ({{ result.best_tag.trade_count }} 笔)
              </span>
            </el-card>
          </el-col>
          <el-col :span="12">
            <el-card shadow="hover" class="highlight-card" v-if="result.worst_tag">
              <span class="hl-label">最差标签</span>
              <el-tag type="danger" size="large">{{ result.worst_tag.tag }}</el-tag>
              <span class="hl-pnl" :style="{ color: pnlColor(result.worst_tag.total_pnl) }">
                {{ result.worst_tag.total_pnl }} ({{ result.worst_tag.trade_count }} 笔)
              </span>
            </el-card>
          </el-col>
        </el-row>

        <el-card style="margin-top: 16px">
          <template #header>标签绩效排名</template>
          <el-table :data="result.tags" size="small" max-height="500">
            <el-table-column prop="tag" label="标签" width="140">
              <template #default="{ row }">
                <el-tag size="small">{{ row.tag }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="trade_count" label="交易数" width="80" />
            <el-table-column prop="total_pnl" label="总 PnL" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.total_pnl) }">{{ row.total_pnl }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="avg_pnl" label="平均 PnL" width="100">
              <template #default="{ row }">
                <span :style="{ color: pnlColor(row.avg_pnl) }">{{ row.avg_pnl }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="win_rate" label="胜率" width="90">
              <template #default="{ row }">{{ (row.win_rate * 100).toFixed(1) }}%</template>
            </el-table-column>
            <el-table-column prop="avg_rating" label="平均评分" width="90">
              <template #default="{ row }">{{ row.avg_rating ?? '-' }}</template>
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
.highlight-card { display: flex; flex-direction: column; align-items: center; gap: 8px; padding: 12px 0; }
.hl-label { font-size: 12px; color: #909399; }
.hl-pnl { font-size: 16px; font-weight: 600; }
</style>
