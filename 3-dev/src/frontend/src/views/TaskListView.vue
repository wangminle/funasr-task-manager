<template>
  <div class="task-list-view">
    <el-row :gutter="16" class="stats-row">
      <el-col :span="6" v-for="stat in stats" :key="stat.label">
        <el-card shadow="never" class="stat-card">
          <div class="stat-value" :style="{ color: stat.color }">{{ stat.value }}</div>
          <div class="stat-label">{{ stat.label }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="mt-16">
      <template #header>
        <div class="card-header">
          <span>任务列表</span>
          <div class="header-actions">
            <el-select v-model="statusFilter" placeholder="按状态筛选" clearable size="default" style="width: 160px;">
              <el-option label="全部" value="" />
              <el-option label="待处理" value="PENDING" />
              <el-option label="预处理中" value="PREPROCESSING" />
              <el-option label="排队中" value="QUEUED" />
              <el-option label="转写中" value="TRANSCRIBING" />
              <el-option label="已完成" value="SUCCEEDED" />
              <el-option label="失败" value="FAILED" />
              <el-option label="已取消" value="CANCELED" />
            </el-select>
            <el-button @click="fetchTasks" :icon="Refresh">刷新</el-button>
          </div>
        </div>
      </template>

      <el-table :data="tasks" stripe v-loading="loading" empty-text="暂无任务">
        <el-table-column prop="task_id" label="任务ID" width="180">
          <template #default="{ row }"><span class="mono">{{ row.task_id.slice(0, 12) }}...</span></template>
        </el-table-column>
        <el-table-column label="文件" min-width="160">
          <template #default="{ row }">{{ row.file_id ? row.file_id.slice(0, 10) + '...' : '-' }}</template>
        </el-table-column>
        <el-table-column prop="language" label="语言" width="80" />
        <el-table-column label="状态" width="130">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" width="180">
          <template #default="{ row }">
            <el-progress :percentage="Math.round(row.progress * 100)" :status="progressStatus(row.status)" :stroke-width="6" />
          </template>
        </el-table-column>
        <el-table-column label="ETA" width="100">
          <template #default="{ row }">{{ row.eta_seconds != null ? formatEta(row.eta_seconds) : '-' }}</template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.status === 'SUCCEEDED'" type="primary" size="small" text @click="downloadResult(row.task_id)">下载结果</el-button>
            <el-button v-if="['PENDING','QUEUED'].includes(row.status)" type="danger" size="small" text @click="handleCancel(row.task_id)">取消</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="pagination-wrap" v-if="total > pageSize">
        <el-pagination v-model:current-page="currentPage" :page-size="pageSize" :total="total" layout="prev, pager, next, total" @current-change="fetchTasks" />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { listTasks, cancelTask, getTaskResult } from '../api'

const tasks = ref([])
const total = ref(0)
const currentPage = ref(1)
const pageSize = 20
const statusFilter = ref('')
const loading = ref(false)
let pollTimer = null

const stats = computed(() => {
  const all = tasks.value
  return [
    { label: '总任务', value: total.value, color: '#303133' },
    { label: '进行中', value: all.filter(t => ['PREPROCESSING','QUEUED','DISPATCHED','TRANSCRIBING'].includes(t.status)).length, color: '#e6a23c' },
    { label: '已完成', value: all.filter(t => t.status === 'SUCCEEDED').length, color: '#67c23a' },
    { label: '失败', value: all.filter(t => t.status === 'FAILED').length, color: '#f56c6c' },
  ]
})

async function fetchTasks() {
  loading.value = true
  try {
    const params = { page: currentPage.value, page_size: pageSize }
    if (statusFilter.value) params.status = statusFilter.value
    const data = await listTasks(params)
    tasks.value = data.items
    total.value = data.total
  } catch (err) {
    ElMessage.error('获取任务列表失败')
  } finally {
    loading.value = false
  }
}

async function handleCancel(taskId) {
  try {
    await cancelTask(taskId)
    ElMessage.success('任务已取消')
    await fetchTasks()
  } catch (err) {
    ElMessage.error(err.response?.data?.detail || '取消失败')
  }
}

async function downloadResult(taskId) {
  try {
    const data = await getTaskResult(taskId, 'txt')
    const blob = new Blob([typeof data === 'string' ? data : JSON.stringify(data, null, 2)], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${taskId}.txt`
    a.click()
    URL.revokeObjectURL(url)
  } catch (err) {
    ElMessage.error('下载失败')
  }
}

function statusTagType(status) {
  const map = { PENDING: 'info', PREPROCESSING: 'warning', QUEUED: 'warning', DISPATCHED: 'warning', TRANSCRIBING: '', SUCCEEDED: 'success', FAILED: 'danger', CANCELED: 'info' }
  return map[status] || 'info'
}
function statusLabel(status) {
  const map = { PENDING: '待处理', PREPROCESSING: '预处理', QUEUED: '排队中', DISPATCHED: '已分配', TRANSCRIBING: '转写中', SUCCEEDED: '已完成', FAILED: '失败', CANCELED: '已取消' }
  return map[status] || status
}
function progressStatus(status) {
  if (status === 'SUCCEEDED') return 'success'
  if (status === 'FAILED') return 'exception'
  return undefined
}
function formatEta(seconds) {
  if (seconds <= 0) return '-'
  if (seconds < 60) return `${seconds}秒`
  return `${Math.round(seconds / 60)}分钟`
}
function formatDate(dateStr) {
  if (!dateStr) return '-'
  return new Date(dateStr).toLocaleString('zh-CN')
}

onMounted(() => { fetchTasks(); pollTimer = setInterval(fetchTasks, 5000) })
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<style scoped>
.task-list-view { max-width: 1100px; margin: 0 auto; }
.stats-row { margin-bottom: 16px; }
.stat-card { text-align: center; }
.stat-value { font-size: 28px; font-weight: 700; }
.stat-label { font-size: 13px; color: #909399; margin-top: 4px; }
.card-header { display: flex; align-items: center; justify-content: space-between; }
.header-actions { display: flex; gap: 8px; align-items: center; }
.mt-16 { margin-top: 16px; }
.mono { font-family: 'Cascadia Code', 'JetBrains Mono', monospace; font-size: 12px; }
.pagination-wrap { margin-top: 16px; display: flex; justify-content: flex-end; }
</style>
