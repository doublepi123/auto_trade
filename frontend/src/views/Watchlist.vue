<template>
  <div class="watchlist-page">
    <div class="page-heading">
      <h3>观察列表</h3>
      <p>多标的行情观察（仅观察，不自动下单）</p>
    </div>

    <el-card style="margin-bottom: 20px">
      <el-form :inline="true" @submit.prevent="handleAdd">
        <el-form-item label="股票代码">
          <el-input v-model="newSymbol" placeholder="例如 AAPL.US" style="width: 180px" />
        </el-form-item>
        <el-form-item label="市场">
          <el-radio-group v-model="newMarket">
            <el-radio value="US">美股</el-radio>
            <el-radio value="HK">港股</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="别名">
          <el-input v-model="newAlias" placeholder="可选别名" style="width: 140px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="adding" :disabled="!newSymbol.trim()" @click="handleAdd">
            添加
          </el-button>
        </el-form-item>
      </el-form>
      <div v-if="addError" style="margin-top: 8px">
        <el-alert :title="addError" type="error" :closable="false" show-icon />
      </div>
    </el-card>

    <el-card>
      <el-table :data="items" v-loading="loading" style="width: 100%">
        <el-table-column prop="symbol" label="代码" width="120" />
        <el-table-column prop="market" label="市场" width="80">
          <template #default="{ row }">
            <el-tag size="small">{{ row.market }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="alias" label="别名" width="140">
          <template #default="{ row }">{{ row.alias || '-' }}</template>
        </el-table-column>
        <el-table-column label="行情" width="180">
          <template #default="{ row }">
            <div v-if="quoteMap[row.symbol]">
              <div>${{ formatNumber(quoteMap[row.symbol].last_price) }}</div>
              <small style="color: #909399">
                Bid {{ formatNumber(quoteMap[row.symbol].bid) }} / Ask {{ formatNumber(quoteMap[row.symbol].ask) }}
              </small>
            </div>
            <div v-else style="color: #909399">-</div>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag v-if="row.is_active" type="success">交易中</el-tag>
            <el-tag v-else type="info">观察中</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <el-button
              v-if="!row.is_active"
              type="primary"
              size="small"
              :loading="activatingId === row.id"
              @click="handleActivate(row.id)"
            >
              设为交易
            </el-button>
            <el-button
              type="danger"
              size="small"
              :loading="removingId === row.id"
              @click="handleRemove(row.id)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="items.length === 0 && !loading" style="text-align: center; color: #909399; padding: 32px">
        暂无观察标的，请添加股票代码
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import type { WatchlistItem, WatchlistQuote } from '../types'
import {
  getWatchlist,
  addWatchlistItem,
  removeWatchlistItem,
  activateWatchlistItem,
  getWatchlistQuotes,
} from '../api/watchlist'

const items = ref<WatchlistItem[]>([])
const quoteMap = ref<Record<string, WatchlistQuote>>({})
const loading = ref(false)
const adding = ref(false)
const addError = ref('')
const activatingId = ref<number | null>(null)
const removingId = ref<number | null>(null)
const newSymbol = ref('')
const newMarket = ref<'US' | 'HK'>('US')
const newAlias = ref('')
let quoteTimer: ReturnType<typeof setInterval> | null = null

async function loadItems() {
  loading.value = true
  try {
    items.value = await getWatchlist()
  } catch (e: any) {
    // ignore
  } finally {
    loading.value = false
  }
}

async function loadQuotes() {
  if (items.value.length === 0) return
  try {
    const quotes = await getWatchlistQuotes()
    const map: Record<string, WatchlistQuote> = {}
    for (const q of quotes) {
      map[q.symbol] = q
    }
    quoteMap.value = map
  } catch (e: any) {
    // ignore
  }
}

async function handleAdd() {
  if (!newSymbol.value.trim()) return
  adding.value = true
  addError.value = ''
  try {
    await addWatchlistItem({
      symbol: newSymbol.value.trim().toUpperCase(),
      market: newMarket.value,
      alias: newAlias.value.trim(),
    })
    newSymbol.value = ''
    newAlias.value = ''
    await loadItems()
    await loadQuotes()
  } catch (e: any) {
    addError.value = e.response?.data?.detail || '添加失败'
  } finally {
    adding.value = false
  }
}

async function handleRemove(id: number) {
  removingId.value = id
  try {
    await removeWatchlistItem(id)
    await loadItems()
    await loadQuotes()
  } catch (e: any) {
    // ignore
  } finally {
    removingId.value = null
  }
}

async function handleActivate(id: number) {
  activatingId.value = id
  try {
    await activateWatchlistItem(id)
    await loadItems()
  } catch (e: any) {
    // ignore
  } finally {
    activatingId.value = null
  }
}

function formatNumber(n: number) {
  if (n === 0) return '0.00'
  return n.toFixed(2)
}

onMounted(() => {
  loadItems().then(() => loadQuotes())
  quoteTimer = setInterval(loadQuotes, 15000)
})

onUnmounted(() => {
  if (quoteTimer) clearInterval(quoteTimer)
})
</script>

<style scoped>
.watchlist-page {
  max-width: 900px;
  margin: 0 auto;
  padding: 16px;
}

.page-heading {
  margin-bottom: 16px;
}

.page-heading h3 {
  margin: 0 0 4px;
}

.page-heading p {
  margin: 0;
  color: #909399;
  font-size: 13px;
}
</style>
