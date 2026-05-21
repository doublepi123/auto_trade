<template>
  <div>
    <h3>凭证设置</h3>
    <el-card v-loading="loading" style="max-width: 600px">
      <el-alert
        type="info"
        title="已保存的凭证不会回显；留空字段会保留现有值，填写后保存将覆盖对应凭证。"
        show-icon
        :closable="false"
        style="margin-bottom: 16px"
      />
      <p v-if="loading" style="color: #999; text-align: center">凭证状态加载中...</p>
      <el-form :model="form" label-width="220px" :disabled="loading" @submit.prevent="handleSave">
        <el-form-item label="长桥应用标识">
          <el-input v-model="form.longbridge_app_key" placeholder="留空则保留当前应用标识">
            <template #suffix v-if="hasFlags.has_longbridge_app_key">
              <el-tag size="small" type="success">已保存</el-tag>
            </template>
          </el-input>
        </el-form-item>
        <el-form-item label="长桥应用密钥">
          <el-input v-model="form.longbridge_app_secret" placeholder="留空则保留当前应用密钥" show-password>
            <template #suffix v-if="hasFlags.has_longbridge_app_secret">
              <el-tag size="small" type="success">已保存</el-tag>
            </template>
          </el-input>
        </el-form-item>
        <el-form-item label="长桥访问令牌">
          <el-input v-model="form.longbridge_access_token" placeholder="留空则保留当前访问令牌" show-password>
            <template #suffix v-if="hasFlags.has_longbridge_access_token">
              <el-tag size="small" type="success">已保存</el-tag>
            </template>
          </el-input>
        </el-form-item>
        <el-form-item label="Server酱推送密钥">
          <el-input v-model="form.sct_key" placeholder="留空则保留当前推送密钥" show-password>
            <template #suffix v-if="hasFlags.has_sct_key">
              <el-tag size="small" type="success">已保存</el-tag>
            </template>
          </el-input>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" native-type="submit" :loading="saving" :disabled="loading || !isDirty">保存</el-button>
          <el-tag v-if="saved" type="success" style="margin-left: 10px">已保存</el-tag>
          <el-tag v-if="error" type="danger" style="margin-left: 10px">{{ error }}</el-tag>
        </el-form-item>
      </el-form>
      <el-alert v-if="reloadWarning" type="warning" :title="reloadWarning" show-icon style="margin-top: 12px" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { getCredentials, updateCredentials } from '../api'

const form = ref({
  longbridge_app_key: '',
  longbridge_app_secret: '',
  longbridge_access_token: '',
  sct_key: '',
})

const hasFlags = ref({
  has_longbridge_app_key: false,
  has_longbridge_app_secret: false,
  has_longbridge_access_token: false,
  has_sct_key: false,
})

const saving = ref(false)
const loading = ref(true)
const saved = ref(false)
const error = ref<string | null>(null)
const reloadWarning = ref<string | null>(null)
const savedSnapshot = ref(serializeForm())

watch(form, () => {
  if (isDirty()) {
    saved.value = false
    error.value = null
  }
}, { deep: true })

onMounted(async () => {
  try {
    const credentials = await getCredentials()
    hasFlags.value = {
      has_longbridge_app_key: credentials.has_longbridge_app_key,
      has_longbridge_app_secret: credentials.has_longbridge_app_secret,
      has_longbridge_access_token: credentials.has_longbridge_access_token,
      has_sct_key: credentials.has_sct_key,
    }
    savedSnapshot.value = serializeForm()
  } catch (e) {
    console.error('加载凭证失败：', e)
    error.value = '加载失败'
  } finally {
    loading.value = false
  }
})

onBeforeRouteLeave(() => {
  if (!isDirty()) return true
  return ElMessageBox.confirm('凭证表单中有未保存的输入，确定要离开当前页面吗？', '未保存的更改', { type: 'warning' })
    .then(() => true)
    .catch(() => false)
})

function serializeForm(): string {
  return JSON.stringify(form.value)
}

function isDirty(): boolean {
  return serializeForm() !== savedSnapshot.value
}

function clearForm() {
  form.value = {
    longbridge_app_key: '',
    longbridge_app_secret: '',
    longbridge_access_token: '',
    sct_key: '',
  }
  savedSnapshot.value = serializeForm()
}

async function handleSave() {
  if (loading.value) return
  saving.value = true
  saved.value = false
  error.value = null
  try {
    const payload = Object.fromEntries(
      Object.entries(form.value).filter(([, value]) => value.trim() !== ''),
    )
    const resp = await updateCredentials(payload)
    hasFlags.value = {
      has_longbridge_app_key: resp.has_longbridge_app_key,
      has_longbridge_app_secret: resp.has_longbridge_app_secret,
      has_longbridge_access_token: resp.has_longbridge_access_token,
      has_sct_key: resp.has_sct_key,
    }
    reloadWarning.value = resp.reload_warning ?? null
    clearForm()
    saved.value = true
  } catch (e) {
    console.error('保存凭证失败：', e)
    error.value = '保存失败'
  } finally {
    saving.value = false
  }
}
</script>
