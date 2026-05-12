<template>
  <div>
    <h3>Strategy Configuration</h3>
    <el-card style="max-width: 600px">
      <el-form :model="form" label-width="180px" @submit.prevent="handleSave">
        <el-form-item label="Symbol">
          <el-input v-model="form.symbol" placeholder="e.g. AAPL.US" />
        </el-form-item>
        <el-form-item label="Market">
          <el-radio-group v-model="form.market">
            <el-radio value="US">US</el-radio>
            <el-radio value="HK">HK</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="Buy Low Price">
          <el-input-number v-model="form.buy_low" :min="0.01" :precision="2" />
        </el-form-item>
        <el-form-item label="Sell High Price">
          <el-input-number v-model="form.sell_high" :min="0.01" :precision="2" />
        </el-form-item>
        <el-form-item label="Short Selling">
          <el-switch v-model="form.short_selling" />
        </el-form-item>
        <el-form-item label="Max Daily Loss">
          <el-input-number v-model="form.max_daily_loss" :min="1" :precision="2" />
        </el-form-item>
        <el-form-item label="Max Consecutive Losses">
          <el-input-number v-model="form.max_consecutive_losses" :min="1" />
        </el-form-item>
        <el-form-item label="ServerChan SCT Key">
          <el-input v-model="form.sct_key" placeholder="SCT key for notifications" show-password />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="handleSave" :loading="saving">Save</el-button>
          <el-tag v-if="saved" type="success" style="margin-left: 10px">Saved!</el-tag>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { getStrategy, updateStrategy } from '../api'

const form = ref({
  symbol: '',
  market: 'US' as 'US' | 'HK',
  buy_low: 0,
  sell_high: 0,
  short_selling: false,
  max_daily_loss: 5000,
  max_consecutive_losses: 3,
  sct_key: '',
})

const saving = ref(false)
const saved = ref(false)

onMounted(async () => {
  try {
    const s = await getStrategy()
    form.value = {
      symbol: s.symbol,
      market: s.market,
      buy_low: s.buy_low,
      sell_high: s.sell_high,
      short_selling: s.short_selling,
      max_daily_loss: s.max_daily_loss,
      max_consecutive_losses: s.max_consecutive_losses,
      sct_key: s.sct_key,
    }
  } catch (e) {
    console.error('Failed to load strategy:', e)
  }
})

async function handleSave() {
  saving.value = true
  saved.value = false
  try {
    await updateStrategy(form.value)
    saved.value = true
  } catch (e) {
    console.error('Save failed:', e)
  } finally {
    saving.value = false
  }
}
</script>
