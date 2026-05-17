<template>
  <div>
    <h3>策略配置</h3>
    <el-card style="max-width: 600px">
      <el-form :model="form" label-width="180px" @submit.prevent="handleSave">
        <el-form-item label="股票代码">
          <el-input v-model="form.symbol" placeholder="例如 AAPL.US" />
        </el-form-item>
        <el-form-item label="市场">
          <el-radio-group v-model="form.market">
            <el-radio value="US">美股</el-radio>
            <el-radio value="HK">港股</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="买入价下限">
          <el-input-number v-model="form.buy_low" :min="0.01" :precision="2" />
        </el-form-item>
        <el-form-item label="卖出价上限">
          <el-input-number v-model="form.sell_high" :min="0.01" :precision="2" />
        </el-form-item>
        <el-form-item label="做空">
          <el-switch v-model="form.short_selling" />
        </el-form-item>
        <el-form-item label="单日最大亏损">
          <el-input-number v-model="form.max_daily_loss" :min="1" :precision="2" />
        </el-form-item>
        <el-form-item label="连续亏损暂停阈值">
          <el-input-number v-model="form.max_consecutive_losses" :min="1" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" native-type="submit" :loading="saving" :disabled="loading">保存</el-button>
          <el-tag v-if="saved" type="success" style="margin-left: 10px">已保存</el-tag>
        </el-form-item>
      </el-form>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getStrategy, updateStrategy } from '../api'

const form = ref({
  symbol: '',
  market: 'US' as 'US' | 'HK',
  buy_low: 0,
  sell_high: 0,
  short_selling: false,
  max_daily_loss: 5000,
  max_consecutive_losses: 3,
})

const saving = ref(false)
const saved = ref(false)
const loading = ref(true)
const savedSnapshot = ref(serializeForm())

watch(form, () => {
  if (isDirty()) {
    saved.value = false
  }
}, { deep: true })

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
    }
    savedSnapshot.value = serializeForm()
  } catch (e) {
    console.error('加载策略失败：', e)
    ElMessage.error('加载策略失败')
  } finally {
    loading.value = false
  }
})

onBeforeRouteLeave(() => {
  if (!isDirty()) return true
  return ElMessageBox.confirm('策略配置尚未保存，确定要离开当前页面吗？', '未保存的更改', { type: 'warning' })
    .then(() => true)
    .catch(() => false)
})

function serializeForm(): string {
  return JSON.stringify(form.value)
}

function isDirty(): boolean {
  return serializeForm() !== savedSnapshot.value
}

async function handleSave() {
  if (!form.value.symbol) {
    ElMessage.error('股票代码不能为空')
    return
  }
  if (form.value.buy_low <= 0) {
    ElMessage.error('买入价下限必须大于 0')
    return
  }
  if (form.value.sell_high <= 0) {
    ElMessage.error('卖出价上限必须大于 0')
    return
  }
  if (form.value.buy_low >= form.value.sell_high) {
    ElMessage.error('买入价下限必须小于卖出价上限')
    return
  }

  saving.value = true
  saved.value = false
  try {
    await updateStrategy(form.value)
    savedSnapshot.value = serializeForm()
    saved.value = true
  } catch (e: any) {
    console.error('保存失败：', e)
    const detail = e?.response?.data?.detail
    if (typeof detail === 'string' && detail) {
      ElMessage.error(`保存失败：${detail}`)
    } else if (Array.isArray(detail)) {
      const msgs = detail.map((d: any) => d.msg || d.message || JSON.stringify(d)).join('；')
      ElMessage.error(`保存失败：${msgs}`)
    } else {
      ElMessage.error('保存失败')
    }
  } finally {
    saving.value = false
  }
}
</script>
