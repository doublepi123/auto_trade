<template>
  <div class="platform-catalog-page" data-testid="platform-catalog-page">
    <div class="page-header">
      <h3>平台分析模块目录</h3>
      <p>浏览平台分析层模块、函数与依赖关系</p>
    </div>

    <div class="catalog-body">
      <el-card class="category-card" shadow="never">
        <template #header>
          <div class="sidebar-header">
            <span>分类</span>
            <el-button size="small" link :disabled="activeCategory === ''" data-testid="catalog-category-all" @click="selectCategory('')">全部</el-button>
          </div>
        </template>
        <div v-loading="categoryLoading" class="category-list">
          <div
            v-for="c in categories"
            :key="c.category"
            :class="['category-item', { 'category-item-active': activeCategory === c.category }]"
            data-testid="catalog-category-item"
            @click="selectCategory(c.category)"
          >
            <span class="category-name">{{ c.category }}</span>
            <el-badge :value="c.count" type="primary" />
          </div>
          <el-empty v-if="!categoryLoading && categories.length === 0" description="无分类" :image-size="40" />
        </div>
      </el-card>

      <div class="main-area">
        <div class="toolbar">
          <el-input
            v-model="searchInput"
            placeholder="搜索模块名 / 描述"
            clearable
            data-testid="catalog-search"
            style="width: 260px"
            @keyup.enter="reloadModules"
            @clear="reloadModules"
          />
          <el-button type="primary" :loading="moduleLoading" data-testid="catalog-refresh" @click="reloadModules">刷新</el-button>
        </div>

        <el-table
          :data="modules"
          stripe
          v-loading="moduleLoading"
          class="responsive-table"
          data-testid="catalog-module-table"
          @row-click="openDetail"
        >
          <el-table-column prop="name" label="模块" min-width="180" />
          <el-table-column prop="category" label="分类" min-width="120" />
          <el-table-column prop="description" label="描述" min-width="240" show-overflow-tooltip />
          <el-table-column prop="function_count" label="函数" width="80" />
          <el-table-column prop="class_count" label="类" width="80" />
        </el-table>

        <el-empty v-if="!moduleLoading && modules.length === 0" description="无匹配模块" />
      </div>
    </div>

    <el-dialog v-model="detailDialog.visible" :title="`模块详情 · ${detailDialog.name}`" width="720px" data-testid="catalog-detail-dialog">
      <div v-loading="detailDialog.loading">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="模块">{{ detailDialog.data?.name }}</el-descriptions-item>
          <el-descriptions-item label="分类">{{ detailDialog.data?.category }}</el-descriptions-item>
          <el-descriptions-item label="行数">{{ detailDialog.data?.line_count ?? '—' }}</el-descriptions-item>
          <el-descriptions-item label="描述">{{ detailDialog.data?.description || '—' }}</el-descriptions-item>
        </el-descriptions>

        <div v-if="detailDialog.data" class="detail-block">
          <div class="block-title">函数 ({{ detailDialog.data.functions.length }})</div>
          <el-table :data="detailDialog.data.functions" size="small" max-height="220">
            <el-table-column prop="name" label="名称" min-width="160" />
            <el-table-column prop="args" label="参数" min-width="200" show-overflow-tooltip />
            <el-table-column prop="return_type" label="返回类型" min-width="120" />
          </el-table>
        </div>

        <div v-if="detailDialog.data" class="detail-block">
          <div class="block-title">类 ({{ detailDialog.data.classes.length }})</div>
          <div class="tag-list">
            <el-tag v-for="c in detailDialog.data.classes" :key="c" size="small">{{ c }}</el-tag>
            <span v-if="detailDialog.data.classes.length === 0" class="hint">—</span>
          </div>
        </div>

        <div v-if="detailDialog.data" class="detail-block">
          <div class="block-title">依赖 ({{ detailDialog.data.imports.length }})</div>
          <div class="tag-list">
            <el-tag v-for="i in detailDialog.data.imports" :key="i" size="small" type="info" effect="plain">{{ i }}</el-tag>
            <span v-if="detailDialog.data.imports.length === 0" class="hint">—</span>
          </div>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { getCategories, getModuleDetail, getModules } from '../api/platformCatalog'
import type { CategoryCount, ModuleDetail, ModuleSummary } from '../api/platformCatalog'
import { resolveErrorMessage } from '../utils/error'

const categories = ref<CategoryCount[]>([])
const modules = ref<ModuleSummary[]>([])
const activeCategory = ref('')
const searchInput = ref('')
const categoryLoading = ref(false)
const moduleLoading = ref(false)

const detailDialog = reactive({
  visible: false,
  loading: false,
  name: '',
  data: null as ModuleDetail | null,
})

async function loadCategories() {
  categoryLoading.value = true
  try {
    categories.value = await getCategories()
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '加载分类失败'))
  } finally {
    categoryLoading.value = false
  }
}

async function reloadModules() {
  moduleLoading.value = true
  const search = searchInput.value.trim() || undefined
  const category = activeCategory.value || undefined
  try {
    modules.value = await getModules({ category, search })
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '加载模块列表失败'))
  } finally {
    moduleLoading.value = false
  }
}

function selectCategory(category: string) {
  activeCategory.value = category
  reloadModules()
}

async function openDetail(row: ModuleSummary) {
  detailDialog.name = row.name
  detailDialog.visible = true
  detailDialog.loading = true
  detailDialog.data = null
  try {
    detailDialog.data = await getModuleDetail(row.name)
  } catch (e) {
    ElMessage.error(resolveErrorMessage(e, '加载模块详情失败'))
  } finally {
    detailDialog.loading = false
  }
}

async function loadAll() {
  await Promise.all([loadCategories(), reloadModules()])
}

onMounted(loadAll)
</script>

<style scoped>
.platform-catalog-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 16px;
  background: #fff;
  min-height: calc(100vh - 120px);
}

.page-header h3 {
  margin: 0;
}

.page-header p {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
}

.catalog-body {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}

.category-card {
  width: 220px;
  flex-shrink: 0;
}

.sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.category-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 520px;
  overflow-y: auto;
}

.category-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 8px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 13px;
}

.category-item:hover {
  background: #f5f7fa;
}

.category-item-active {
  background: #ecf5ff;
  color: #409eff;
}

.category-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.main-area {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.toolbar {
  display: flex;
  gap: 8px;
  align-items: center;
}

.responsive-table {
  width: 100%;
}

.detail-block {
  margin-top: 16px;
}

.block-title {
  font-size: 13px;
  font-weight: 600;
  margin-bottom: 8px;
  color: #303133;
}

.tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.hint {
  color: #909399;
  font-size: 13px;
}

:deep(.el-table__row) {
  cursor: pointer;
}

@media (max-width: 720px) {
  .catalog-body {
    flex-direction: column;
  }

  .category-card {
    width: 100%;
  }
}
</style>
