<template>
  <el-container>
    <el-header>
      <h2>Auto Trade</h2>
      <el-menu mode="horizontal" :default-active="route.path" router>
        <el-menu-item index="/">仪表盘</el-menu-item>
        <el-menu-item index="/strategy">策略配置</el-menu-item>
        <el-menu-item index="/credentials">凭证设置</el-menu-item>
        <el-menu-item index="/history">交易历史</el-menu-item>
      </el-menu>
      <el-button text @click="showApiKeyDialog = true" style="margin-left: auto">
        {{ hasApiKey ? 'API 密钥已设置' : '设置 API 密钥' }}
      </el-button>
    </el-header>
    <el-main>
      <router-view />
    </el-main>

    <el-dialog v-model="showApiKeyDialog" title="设置 API 密钥" width="400px" :close-on-click-modal="false">
      <el-form @submit.prevent="saveApiKey">
        <el-form-item label="API Key">
          <el-input v-model="apiKeyInput" placeholder="输入 API 密钥" show-password />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showApiKeyDialog = false">取消</el-button>
        <el-button type="primary" @click="saveApiKey" :disabled="!apiKeyInput">保存</el-button>
        <el-button v-if="hasApiKey" type="danger" plain @click="clearApiKey">清除</el-button>
      </template>
    </el-dialog>
  </el-container>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'

const route = useRoute()

const showApiKeyDialog = ref(false)
const apiKeyInput = ref('')

const hasApiKey = ref(!!localStorage.getItem('api_key'))

function onApiKeyRequired() {
  showApiKeyDialog.value = true
  hasApiKey.value = false
  ElMessage.error('API 密钥无效，请重新设置')
}

onMounted(() => {
  window.addEventListener('api-key-required', onApiKeyRequired)
})

onUnmounted(() => {
  window.removeEventListener('api-key-required', onApiKeyRequired)
})

function saveApiKey() {
  if (apiKeyInput.value) {
    localStorage.setItem('api_key', apiKeyInput.value)
    hasApiKey.value = true
    apiKeyInput.value = ''
    showApiKeyDialog.value = false
    ElMessage.success('API 密钥已保存')
  }
}

function clearApiKey() {
  localStorage.removeItem('api_key')
  hasApiKey.value = false
  apiKeyInput.value = ''
  showApiKeyDialog.value = false
  ElMessage.success('API 密钥已清除')
}
