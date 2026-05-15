<template>
  <div>
    <h3>凭证设置</h3>
    <el-card style="max-width: 600px">
      <el-form :model="form" label-width="220px" @submit.prevent="handleSave">
        <el-form-item label="长桥应用标识">
          <el-input v-model="form.longbridge_app_key" placeholder="留空则保留当前应用标识">
            <template #suffix v-if="hasFlags.has_longbridge_app_key">
              <el-tag size="small" type="success">已配置</el-tag>
            </template>
          </el-input>
        </el-form-item>
        <el-form-item label="长桥应用密钥">
          <el-input v-model="form.longbridge_app_secret" placeholder="留空则保留当前应用密钥" show-password>
            <template #suffix v-if="hasFlags.has_longbridge_app_secret">
              <el-tag size="small" type="success">已配置</el-tag>
            </template>
          </el-input>
        </el-form-item>
        <el-form-item label="长桥访问令牌">
          <el-input v-model="form.longbridge_access_token" placeholder="留空则保留当前访问令牌" show-password>
            <template #suffix v-if="hasFlags.has_longbridge_access_token">
              <el-tag size="small" type="success">已配置</el-tag>
            </template>
          </el-input>
        </el-form-item>
        <el-form-item label="Server酱推送密钥">
          <el-input v-model="form.sct_key" placeholder="留空则保留当前推送密钥" show-password>
            <template #suffix v-if="hasFlags.has_sct_key">
              <el-tag size="small" type="success">已配置</el-tag>
            </template>
          </el-input>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" native-type="submit" :loading="saving" :disabled="loading">保存</el-button>
          <el-tag v-if="saved" type="success" style="margin-left: 10px">已保存</el-tag>
        </el-form-item>
      </el-form>
      <el-alert v-if="reloadWarning" type="warning" :title="reloadWarning" show-icon style="margin-top: 12px" />
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
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
const reloadWarning = ref<string | null>(null)

watch(form, () => {
  saved.value = false
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
  } catch (e) {
    console.error('加载凭证失败：', e)
    ElMessage.error('加载凭证失败')
  } finally {
    loading.value = false
  }
})

async function handleSave() {
  if (loading.value) return
  saving.value = true
  saved.value = false
  try {
    const resp = await updateCredentials(form.value)
    hasFlags.value = {
      has_longbridge_app_key: resp.has_longbridge_app_key,
      has_longbridge_app_secret: resp.has_longbridge_app_secret,
      has_longbridge_access_token: resp.has_longbridge_access_token,
      has_sct_key: resp.has_sct_key,
    }
    reloadWarning.value = resp.reload_warning ?? null
    saved.value = true
  } catch (e) {
    console.error('保存凭证失败：', e)
    ElMessage.error('保存凭证失败')
  } finally {
    saving.value = false
  }
}
</script>
