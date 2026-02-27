<template>
  <div class="task-detail-view">
    <el-page-header @back="$router.push('/tasks')" title="返回列表" :content="`任务详情 - ${taskId.slice(0, 12)}...`" />

    <el-row :gutter="16" class="mt-16">
      <el-col :span="16">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <span>任务信息</span>
              <el-tag :type="statusTagType(task.status)" size="large">{{ statusLabel(task.status) }}</el-tag>
            </div>
          </template>
          <el-descriptions :column="2" border>
            <el-descriptions-item label="任务ID"><span class="mono">{{ task.task_id }}</span></el-descriptions-item>
            <el-descriptions-item label="文件ID"><span class="mono">{{ task.file_id }}</span></el-descriptions-item>
            <el-descriptions-item label="语言">{{ task.language }}</el-descriptions-item>
            <el-descriptions-item label="分配服务器">{{ task.assigned_server_id || '未分配' }}</el-descriptions-item>
            <el-descriptions-item label="创建时间">{{ formatDate(task.created_at) }}</el-descriptions-item>
            <el-descriptions-item label="开始时间">{{ formatDate(task.started_at) }}</el-descriptions-item>
            <el-descriptions-item label="完成时间">{{ formatDate(task.completed_at) }}</el-descriptions-item>
            <el-descriptions-item label="重试次数">{{ task.retry_count }}</el-descriptions-item>
          </el-descriptions>

          <div v-if="task.error_message" class="error-section mt-16">
            <el-alert :title="task.error_code || '错误'" :description="task.error_message" type="error" show-icon :closable="false" />
          </div>
        </el-card>

        <el-card shadow="never" class="mt-16">
          <template #header><span>进度追踪</span></template>
          <div class="progress-section">
            <el-progress :percentage="Math.round(progress * 100)" :status="progressStatus(task.status)" :stroke-width="20" :text-inside="true" />
            <div class="progress-info">
              <span class="progress-msg">{{ progressMessage }}</span>
              <span v-if="eta != null && eta > 0" class="eta">预计剩余: {{ formatEta(eta) }}</span>
            </div>
          </div>

          <el-timeline class="mt-16">
            <el-timeline-item v-for="evt in events" :key="evt.timestamp" :timestamp="evt.timestamp" :type="evt.type" placement="top">
              {{ evt.message }}
            </el-timeline-item>
          </el-timeline>
        </el-card>
      </el-col>

      <el-col :span="8">
        <el-card shadow="never">
          <template #header><span>操作</span></template>
          <el-space direction="vertical" fill style="width: 100%;">
            <el-button v-if="task.status === 'SUCCEEDED'" type="primary" @click="downloadResult('json')" style="width: 100%;">
              <el-icon><Download /></el-icon> 下载 JSON
            </el-button>
            <el-button v-if="task.status === 'SUCCEEDED'" type="primary" @click="downloadResult('txt')" style="width: 100%;">
              <el-icon><Document /></el-icon> 下载 TXT
            </el-button>
            <el-button v-if="task.status === 'SUCCEEDED'" type="primary" @click="downloadResult('srt')" style="width: 100%;">
              <el-icon><VideoCamera /></el-icon> 下载 SRT
            </el-button>
            <el-button v-if="canCancel" type="danger" @click="handleCancel" style="width: 100%;">
              <el-icon><CircleClose /></el-icon> 取消任务
            </el-button>
          </el-space>
        </el-card>

        <el-card shadow="never" class="mt-16" v-if="fileInfo">
          <template #header><span>文件信息</span></template>
          <el-descriptions :column="1" border>
            <el-descriptions-item label="文件名">{{ fileInfo.original_name }}</el-descriptions-item>
            <el-descriptions-item label="大小">{{ formatSize(fileInfo.size_bytes) }}</el-descriptions-item>
            <el-descriptions-item label="时长">{{ fileInfo.duration_sec ? formatDuration(fileInfo.duration_sec) : '-' }}</el-descriptions-item>
            <el-descriptions-item label="格式">{{ fileInfo.codec || '-' }}</el-descriptions-item>
            <el-descriptions-item label="采样率">{{ fileInfo.sample_rate ? `${fileInfo.sample_rate} Hz` : '-' }}</el-descriptions-item>
            <el-descriptions-item label="声道">{{ fileInfo.channels || '-' }}</el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Download, Document, VideoCamera, CircleClose } from '@element-plus/icons-vue'
import { getTask, getFileMetadata, getTaskResult, cancelTask, getApiKey } from '../api'

const route = useRoute()
const taskId = route.params.taskId

const task = ref({ task_id: taskId, status: 'PENDING', progress: 0, file_id: '', language: 'zh', retry_count: 0 })
const fileInfo = ref(null)
const progress = ref(0)
const eta = ref(null)
const progressMessage = ref('加载中...')
const events = ref([])
let eventSource = null
let pollTimer = null

const canCancel = computed(() => ['PENDING', 'QUEUED'].includes(task.value.status))

async function loadTask() {
  try {
    const data = await getTask(taskId)
    task.value = data
    progress.value = data.progress
    if (data.file_id && !fileInfo.value) {
      try {
        fileInfo.value = await getFileMetadata(data.file_id)
      } catch {}
    }
  } catch (err) {
    ElMessage.error('获取任务失败')
  }
}

let sseAbortController = null

function connectSSE() {
  if (sseAbortController) sseAbortController.abort()
  if (eventSource) { eventSource.close(); eventSource = null }

  const apiKey = getApiKey()
  const tokenParam = apiKey ? `?token=${encodeURIComponent(apiKey)}` : ''
  const url = `/api/v1/tasks/${taskId}/progress${tokenParam}`

  sseAbortController = new AbortController()

  fetch(url, { signal: sseAbortController.signal, headers: apiKey ? { 'X-API-Key': apiKey } : {} })
    .then(response => {
      if (!response.ok) {
        if (response.status === 401) ElMessage.error('SSE 认证失败，请设置 API Key')
        throw new Error(`SSE HTTP ${response.status}`)
      }
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      function pump() {
        return reader.read().then(({ done, value }) => {
          if (done) return
          buffer += decoder.decode(value, { stream: true })
          const parts = buffer.split('\n\n')
          buffer = parts.pop()
          for (const part of parts) {
            if (!part.trim() || part.startsWith(':')) continue
            let eventType = 'message', eventData = ''
            for (const line of part.split('\n')) {
              if (line.startsWith('event: ')) eventType = line.slice(7)
              else if (line.startsWith('data: ')) eventData = line.slice(6)
            }
            if (!eventData) continue
            try {
              const data = JSON.parse(eventData)
              handleSSEEvent(eventType, data)
            } catch {}
          }
          return pump()
        })
      }
      return pump()
    })
    .catch(err => {
      if (err.name === 'AbortError') return
      if (!['SUCCEEDED', 'FAILED', 'CANCELED'].includes(task.value.status)) {
        setTimeout(connectSSE, 3000)
      }
    })
}

function handleSSEEvent(eventType, data) {
  if (eventType === 'status_change') {
    task.value.status = data.status
    progress.value = data.progress
    eta.value = data.eta_seconds
    progressMessage.value = data.message
    events.value.unshift({ timestamp: new Date(data.timestamp).toLocaleTimeString('zh-CN'), message: data.message, type: data.status === 'FAILED' ? 'danger' : data.status === 'SUCCEEDED' ? 'success' : 'primary' })
  } else if (eventType === 'progress_update') {
    progress.value = data.progress
    eta.value = data.eta_seconds
    progressMessage.value = data.message
  } else if (eventType === 'complete') {
    events.value.unshift({ timestamp: new Date(data.timestamp).toLocaleTimeString('zh-CN'), message: `最终状态: ${data.final_status}`, type: data.final_status === 'SUCCEEDED' ? 'success' : 'danger' })
    if (sseAbortController) sseAbortController.abort()
    loadTask()
  }
}

async function downloadResult(format) {
  try {
    const data = await getTaskResult(taskId, format)
    const blob = new Blob([typeof data === 'string' ? data : JSON.stringify(data, null, 2)], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${taskId}.${format}`
    a.click()
    URL.revokeObjectURL(url)
  } catch (err) {
    ElMessage.error('下载失败')
  }
}

async function handleCancel() {
  try {
    await cancelTask(taskId)
    ElMessage.success('已取消')
    await loadTask()
  } catch (err) {
    ElMessage.error(err.response?.data?.detail || '取消失败')
  }
}

function statusTagType(s) { return { PENDING: 'info', PREPROCESSING: 'warning', QUEUED: 'warning', DISPATCHED: 'warning', TRANSCRIBING: '', SUCCEEDED: 'success', FAILED: 'danger', CANCELED: 'info' }[s] || 'info' }
function statusLabel(s) { return { PENDING: '待处理', PREPROCESSING: '预处理', QUEUED: '排队中', DISPATCHED: '已分配', TRANSCRIBING: '转写中', SUCCEEDED: '已完成', FAILED: '失败', CANCELED: '已取消' }[s] || s }
function progressStatus(s) { if (s === 'SUCCEEDED') return 'success'; if (s === 'FAILED') return 'exception'; return undefined }
function formatDate(d) { return d ? new Date(d).toLocaleString('zh-CN') : '-' }
function formatEta(s) { if (s < 60) return `${s}秒`; return `${Math.round(s / 60)}分钟` }
function formatSize(b) { if (b < 1048576) return (b / 1024).toFixed(1) + ' KB'; return (b / 1048576).toFixed(1) + ' MB' }
function formatDuration(s) { const m = Math.floor(s / 60); const sec = Math.floor(s % 60); return `${m}分${sec}秒` }

onMounted(() => { loadTask(); connectSSE(); pollTimer = setInterval(loadTask, 10000) })
onUnmounted(() => { if (sseAbortController) sseAbortController.abort(); if (eventSource) eventSource.close(); if (pollTimer) clearInterval(pollTimer) })
</script>

<style scoped>
.task-detail-view { max-width: 1100px; margin: 0 auto; }
.card-header { display: flex; align-items: center; justify-content: space-between; }
.mt-16 { margin-top: 16px; }
.mono { font-family: 'Cascadia Code', monospace; font-size: 12px; }
.progress-section { padding: 8px 0; }
.progress-info { display: flex; justify-content: space-between; margin-top: 8px; }
.progress-msg { color: #606266; }
.eta { color: #909399; font-size: 13px; }
.error-section { margin-top: 16px; }
</style>
