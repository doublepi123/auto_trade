<template>
  <div>
    <h3>凭证设置</h3>
    <el-card style="max-width: 600px">
      <p style="margin-top: 0; color: var(--el-text-color-secondary)">留空表示保留当前凭证；如需清除，请使用清除按钮。</p>
      <el-form :model="form" label-width="220px" @submit.prevent="handleSave">
        <el-form-item label="长桥应用标识">
          <el-input v-model="form.longbridge_app_key" placeholder="留空则保留当前应用标识">
            <template #suffix v-if="hasFlags.has_longbridge_app_key">
              <el-tag size="small" type="success">已配置</el-tag>
            </template>
          </el-input>
          <el-button v-if="hasFlags.has_longbridge_app_key" type="danger" plain :loading="clearingField === 'longbridge_app_key'" :disabled="loading || saving" style="margin-left: 8px" @click="handleClear('longbridge_app_key')">清除</el-button>
        </el-form-item>
        <el-form-item label="长桥应用密钥">
          <el-input v-model="form.longbridge_app_secret" placeholder="留空则保留当前应用密钥" show-password>
            <template #suffix v-if="hasFlags.has_longbridge_app_secret">
              <el-tag size="small" type="success">已配置</el-tag>
            </template>
          </el-input>
          <el-button v-if="hasFlags.has_longbridge_app_secret" type="danger" plain :loading="clearingField === 'longbridge_app_secret'" :disabled="loading || saving" style="margin-left: 8px" @click="handleClear('longbridge_app_secret')">清除</el-button>
        </el-form-item>
        <el-form-item label="长桥访问令牌">
          <el-input v-model="form.longbridge_access_token" placeholder="留空则保留当前访问令牌" show-password>
            <template #suffix v-if="hasFlags.has_longbridge_access_token">
              <el-tag size="small" type="success">已配置</el-tag>
            </template>
          </el-input>
          <el-button v-if="hasFlags.has_longbridge_access_token" type="danger" plain :loading="clearingField === 'longbridge_access_token'" :disabled="loading || saving" style="margin-left: 8px" @click="handleClear('longbridge_access_token')">清除</el-button>
        </el-form-item>
        <el-form-item label="Server酱推送密钥">
          <el-input v-model="form.sct_key" placeholder="留空则保留当前推送密钥" show-password>
            <template #suffix v-if="hasFlags.has_sct_key">
              <el-tag size="small" type="success">已配置</el-tag>
            </template>
          </el-input>
          <el-button v-if="hasFlags.has_sct_key" type="danger" plain :loading="clearingField === 'sct_key'" :disabled="loading || saving" style="margin-left: 8px" @click="handleClear('sct_key')">清除</el-button>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" native-type="submit" :loading="saving" :disabled="loading">保存</el-button>
          <el-tag v-if="saved" type="success" style="margin-left: 10px">已保存</el-tag>
        </el-form-item>
      </el-form>
      <el-alert v-if="reloadWarning" type="warning" :title="reloadWarning" show-icon style="margin-top: 12px" />
    </el-card>
    <el-alert v-if="error" type="error" :title="error" show-icon style="max-width: 600px; margin-top: 12px" />
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { getCredentials, updateCredentials } from '../api'
import { useFormSaveState } from '../composables/useFormSaveState'
import type { CredentialsConfig } from '../types'

type CredentialField = 'longbridge_app_key' | 'longbridge_app_secret' | 'longbridge_access_token' | 'sct_key'

const credentialFields: CredentialField[] = [
  'longbridge_app_key',
  'longbridge_app_secret',
  'longbridge_access_token',
  'sct_key',
]

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

const { loading, saving, saved, error, markDirty, beginSave, saveSucceeded, saveFailed } = useFormSaveState()
const reloadWarning = ref<string | null>(null)
const clearingField = ref<CredentialField | null>(null)

watch(form, () => {
  markDirty()
}, { deep: true })

function updateHasFlags(credentials: CredentialsConfig) {
  hasFlags.value = {
    has_longbridge_app_key: credentials.has_longbridge_app_key,
    has_longbridge_app_secret: credentials.has_longbridge_app_secret,
    has_longbridge_access_token: credentials.has_longbridge_access_token,
    has_sct_key: credentials.has_sct_key,
  }
}

onMounted(async () => {
  try {
    const credentials = await getCredentials()
    updateHasFlags(credentials)
  } catch (e) {
    console.error('加载凭证失败：', e)
    error.value = '加载凭证失败'
    ElMessage.error('加载凭证失败')
  } finally {
    loading.value = false
  }
})

async function handleSave() {
  if (loading.value || saving.value) return
  const payload: Partial<Record<CredentialField, string>> = {}
  credentialFields.forEach((field) => {
    if (form.value[field]) {
      payload[field] = form.value[field]
    }
  })
  if (Object.keys(payload).length === 0) {
    ElMessage.info('没有需要保存的凭证变更')
    return
  }

  beginSave()
  try {
    const resp = await updateCredentials(payload)
    updateHasFlags(resp)
    reloadWarning.value = resp.reload_warning ?? null
    saveSucceeded()
  } catch (e) {
    console.error('保存凭证失败：', e)
    saveFailed('保存凭证失败')
    ElMessage.error('保存凭证失败')
  } finally {
    saving.value = false
  }
}

async function handleClear(field: CredentialField) {
  if (loading.value || saving.value || clearingField.value) return
  beginSave()
  clearingField.value = field
  try {
    const resp = await updateCredentials({ [field]: '' })
    updateHasFlags(resp)
    reloadWarning.value = resp.reload_warning ?? null
    saveSucceeded()
    ElMessage.success('已清除凭证')
  } catch (e) {
    console.error('清除凭证失败：', e)
    saveFailed('清除凭证失败')
    ElMessage.error('清除凭证失败')
  } finally {
    clearingField.value = null
  }
}
</script>
