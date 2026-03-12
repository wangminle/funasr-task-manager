<template>
  <div class="upload-view">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <el-icon :size="20"><Upload /></el-icon>
          <span>文件上传</span>
        </div>
      </template>
      <el-upload ref="uploadRef" class="upload-area" drag multiple :auto-upload="false" :on-change="handleFileChange" :on-remove="handleFileRemove" accept=".wav,.mp3,.mp4,.flac,.ogg,.webm,.m4a,.aac,.mkv,.avi,.mov">
        <el-icon class="el-icon--upload" :size="48"><UploadFilled /></el-icon>
        <div class="el-upload__text">将文件拖到此处，或 <em>点击上传</em></div>
        <template #tip>
          <div class="el-upload__tip">支持 WAV / MP3 / MP4 / FLAC / OGG / WebM 等音视频格式，单文件最大 2GB</div>
        </template>
      </el-upload>
    </el-card>

    <el-card v-if="pendingFiles.length > 0" shadow="never" class="mt-16">
      <template #header>
        <div class="card-header">
          <span>待上传文件 ({{ pendingFiles.length }})</span>
          <div>
            <el-select v-model="language" size="small" style="width: 100px; margin-right: 8px;">
              <el-option label="中文" value="zh" />
              <el-option label="英文" value="en" />
              <el-option label="日文" value="ja" />
              <el-option label="自动检测" value="auto" />
            </el-select>
            <el-button type="primary" @click="submitAll" :loading="submitting">
              <el-icon><Check /></el-icon> 提交转写
            </el-button>
          </div>
        </div>
      </template>
      <el-table :data="pendingFiles" stripe>
        <el-table-column prop="name" label="文件名" />
        <el-table-column prop="size" label="大小" width="120">
          <template #default="{ row }">{{ formatSize(row.size) }}</template>
        </el-table-column>
        <el-table-column label="上传状态" width="150">
          <template #default="{ row }">
            <el-tag v-if="row.uploadStatus === 'pending'" type="info">待上传</el-tag>
            <el-tag v-else-if="row.uploadStatus === 'uploading'" type="warning">上传中</el-tag>
            <el-tag v-else-if="row.uploadStatus === 'uploaded'" type="success">已上传</el-tag>
            <el-tag v-else-if="row.uploadStatus === 'error'" type="danger">失败</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" width="220">
          <template #default="{ row }">
            <el-progress v-if="row.uploadStatus === 'uploading'" :percentage="row.uploadProgress" :stroke-width="8" :text-inside="true" />
            <el-progress v-else-if="row.uploadStatus === 'uploaded'" :percentage="100" status="success" :stroke-width="8" :text-inside="true" />
            <el-progress v-else-if="row.uploadStatus === 'error'" :percentage="row.uploadProgress || 0" status="exception" :stroke-width="8" :text-inside="true" />
            <span v-else style="color: #909399;">等待上传</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card v-if="createdTasks.length > 0" shadow="never" class="mt-16">
      <template #header>
        <div class="card-header">
          <span>已创建任务 ({{ createdTasks.length }})</span>
          <el-button type="primary" text @click="$router.push('/tasks')">查看任务列表 →</el-button>
        </div>
      </template>
      <el-table :data="createdTasks" stripe>
        <el-table-column prop="task_id" label="任务ID" width="200">
          <template #default="{ row }">{{ row.task_id.slice(0, 16) }}...</template>
        </el-table-column>
        <el-table-column label="状态" width="140">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.status)">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="文件ID">
          <template #default="{ row }">{{ row.file_id.slice(0, 16) }}...</template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { uploadFile, createTasks } from '../api'

const uploadRef = ref(null)
const pendingFiles = ref([])
const createdTasks = ref([])
const language = ref('zh')
const submitting = ref(false)

const MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024

function handleFileChange(uploadFile) {
  const exists = pendingFiles.value.find(f => f.name === uploadFile.name && f.size === uploadFile.raw.size)
  if (exists) return
  if (uploadFile.raw.size > MAX_FILE_SIZE) {
    ElMessage.warning(`文件 "${uploadFile.name}" 超过 2GB 限制，已跳过`)
    return
  }
  pendingFiles.value.push({ name: uploadFile.name, size: uploadFile.raw.size, raw: uploadFile.raw, uploadStatus: 'pending', uploadProgress: 0, fileId: null })
}

function handleFileRemove(uploadFile) {
  const idx = pendingFiles.value.findIndex(f => f.name === uploadFile.name)
  if (idx !== -1) pendingFiles.value.splice(idx, 1)
}

async function submitAll() {
  if (pendingFiles.value.length === 0) return
  submitting.value = true
  try {
    for (const f of pendingFiles.value) {
      if (f.uploadStatus === 'uploaded' || f.uploadStatus === 'error') continue
      f.uploadStatus = 'uploading'
      f.uploadProgress = 0
      try {
        const result = await uploadFile(f.raw, (pct) => { f.uploadProgress = pct })
        f.fileId = result.file_id
        f.uploadStatus = 'uploaded'
        f.uploadProgress = 100
      } catch (err) {
        f.uploadStatus = 'error'
        ElMessage.error(`上传失败: ${f.name}`)
      }
    }
    const uploadedFiles = pendingFiles.value.filter(f => f.fileId)
    if (uploadedFiles.length === 0) { ElMessage.error('没有成功上传的文件'); return }
    const items = uploadedFiles.map(f => ({ file_id: f.fileId, language: language.value }))
    const tasks = await createTasks(items)
    createdTasks.value = tasks
    pendingFiles.value = []
    uploadRef.value?.clearFiles()
    ElMessage.success(`成功创建 ${tasks.length} 个转写任务`)
  } catch (err) {
    ElMessage.error('提交失败: ' + (err.response?.data?.detail || err.message))
  } finally {
    submitting.value = false
  }
}

function formatSize(bytes) {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB'
  if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB'
  return (bytes / 1073741824).toFixed(2) + ' GB'
}

function statusTagType(status) {
  const map = { PENDING: 'info', PREPROCESSING: 'warning', QUEUED: 'warning', DISPATCHED: 'warning', TRANSCRIBING: '', SUCCEEDED: 'success', FAILED: 'danger', CANCELED: 'info' }
  return map[status] || 'info'
}
</script>

<style scoped>
.upload-view { max-width: 960px; }
.card-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.mt-16 { margin-top: 16px; }
.upload-area { width: 100%; }
</style>
