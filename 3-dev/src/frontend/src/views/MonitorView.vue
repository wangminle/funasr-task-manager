<template>
  <div class="monitor-view">
    <el-row :gutter="12" class="stats-row">
      <el-col :xs="12" :sm="8" :md="4" v-for="card in statCards" :key="card.label">
        <el-card shadow="never" class="stat-card">
          <div class="stat-icon"><el-icon :size="22" :color="card.color"><component :is="card.icon" /></el-icon></div>
          <div class="stat-value" :style="{ color: card.color }">{{ card.value }}</div>
          <div class="stat-label">{{ card.label }}</div>
        </el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="mt-16">
      <template #header>
        <div class="card-header">
          <span>ASR 服务器节点</span>
          <div>
            <el-button type="primary" :icon="Plus" @click="openAddDialog">添加</el-button>
            <el-button @click="fetchServers" :icon="Refresh">刷新</el-button>
          </div>
        </div>
      </template>
      <el-table :data="servers" stripe v-loading="loading">
        <el-table-column prop="server_id" label="节点ID" width="140" />
        <el-table-column prop="name" label="名称" width="130">
          <template #default="{ row }">{{ row.name || '-' }}</template>
        </el-table-column>
        <el-table-column label="地址" width="190">
          <template #default="{ row }">{{ row.host }}:{{ row.port }}</template>
        </el-table-column>
        <el-table-column prop="protocol_version" label="协议" width="80" />
        <el-table-column label="并发" width="60">
          <template #default="{ row }">{{ row.max_concurrency }}</template>
        </el-table-column>
        <el-table-column label="RTF" width="70">
          <template #default="{ row }">{{ row.rtf_baseline ? row.rtf_baseline.toFixed(2) : '-' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="serverStatusType(row.status)" effect="dark" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="断路器" width="90">
          <template #default="{ row }">
            <el-tag :type="circuitBreakerType(row.circuit_breaker)" size="small">{{ row.circuit_breaker || 'CLOSED' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最后心跳" width="170">
          <template #default="{ row }">{{ row.last_heartbeat ? new Date(row.last_heartbeat).toLocaleString('zh-CN') : '从未' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="240" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="primary" :loading="row._probing === 'connect'" @click="handleProbe(row, 'connect_only')">连接</el-button>
            <el-button size="small" type="warning" :loading="row._probing === 'speed'" @click="handleProbe(row, 'offline_light')">测速</el-button>
            <el-button size="small" @click="openEditDialog(row)">编辑</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card shadow="never" class="mt-16">
      <template #header>
        <div class="card-header">
          <span>任务统计概览</span>
        </div>
      </template>
      <el-row :gutter="16">
        <el-col :span="12">
          <el-descriptions :column="1" border title="按状态统计">
            <el-descriptions-item v-for="(count, status) in taskStatusCounts" :key="status" :label="statusLabel(status)">
              <el-tag :type="statusTagType(status)" size="small">{{ count }}</el-tag>
            </el-descriptions-item>
          </el-descriptions>
        </el-col>
        <el-col :span="12">
          <el-descriptions :column="1" border title="系统信息">
            <el-descriptions-item label="总节点数">{{ servers.length }}</el-descriptions-item>
            <el-descriptions-item label="总并发槽位">{{ totalSlots }}</el-descriptions-item>
            <el-descriptions-item label="数据刷新间隔">5 秒</el-descriptions-item>
          </el-descriptions>
        </el-col>
      </el-row>
    </el-card>

    <!-- 添加/编辑 服务器对话框 -->
    <el-dialog v-model="dialogVisible" :title="isEditing ? '编辑服务器' : '添加服务器'" width="500px" @close="resetForm">
      <el-form ref="formRef" :model="form" :rules="formRules" label-width="90px">
        <el-form-item label="节点ID" prop="server_id">
          <el-input v-model="form.server_id" :disabled="isEditing" placeholder="例如 asr-10095" />
        </el-form-item>
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" placeholder="例如 FunASR-10095" />
        </el-form-item>
        <el-form-item label="主机地址" prop="host">
          <el-input v-model="form.host" placeholder="例如 100.116.250.20" />
        </el-form-item>
        <el-form-item label="端口" prop="port">
          <el-input-number v-model="form.port" :min="1" :max="65535" style="width:100%;" />
        </el-form-item>
        <el-form-item label="协议版本" prop="protocol_version">
          <el-select v-model="form.protocol_version" style="width:100%;">
            <el-option label="funasr-main" value="funasr-main" />
            <el-option label="funasr-legacy" value="funasr-legacy" />
          </el-select>
        </el-form-item>
        <el-form-item label="最大并发" prop="max_concurrency">
          <el-input-number v-model="form.max_concurrency" :min="1" :max="64" style="width:100%;" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="handleSubmit">{{ isEditing ? '保存' : '添加' }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, markRaw } from 'vue'
import { Refresh, Plus, Monitor as MonitorIcon, WarningFilled, List, Timer, SuccessFilled, TrendCharts } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { listServers, listTasks, probeServer, registerServer, updateServer, getSystemStats } from '../api'

const servers = ref([])
const tasks = ref([])
const sysStats = ref({
  server_total: 0, server_online: 0, slots_total: 0, slots_used: 0,
  queue_depth: 0, tasks_today_completed: 0, tasks_today_failed: 0,
  success_rate_24h: 100, avg_rtf: null,
})
const loading = ref(false)
let pollTimer = null

const totalSlots = computed(() => sysStats.value.slots_total)
const taskStatusCounts = computed(() => {
  const counts = {}
  for (const t of tasks.value) { counts[t.status] = (counts[t.status] || 0) + 1 }
  return counts
})

const statCards = computed(() => [
  { label: '在线节点', value: sysStats.value.server_online, color: '#67c23a', icon: markRaw(MonitorIcon) },
  { label: '槽位 (已用/总)', value: `${sysStats.value.slots_used}/${sysStats.value.slots_total}`, color: '#409eff', icon: markRaw(List) },
  { label: '队列深度', value: sysStats.value.queue_depth, color: '#e6a23c', icon: markRaw(Timer) },
  { label: '今日完成', value: sysStats.value.tasks_today_completed, color: '#67c23a', icon: markRaw(SuccessFilled) },
  { label: '今日失败', value: sysStats.value.tasks_today_failed, color: '#f56c6c', icon: markRaw(WarningFilled) },
  { label: '24h 成功率', value: `${sysStats.value.success_rate_24h}%`, color: sysStats.value.success_rate_24h >= 90 ? '#67c23a' : '#f56c6c', icon: markRaw(TrendCharts) },
])

async function fetchServers() {
  loading.value = true
  try { servers.value = await listServers() } catch { ElMessage.error('获取服务器列表失败') } finally { loading.value = false }
}
async function fetchTasks() {
  try { const data = await listTasks({ page: 1, page_size: 200 }); tasks.value = data.items } catch (err) { console.warn('获取任务列表失败', err) }
}
async function fetchStats() {
  try { sysStats.value = await getSystemStats() } catch (err) { console.warn('获取统计数据失败', err) }
}
function serverStatusType(s) { return { ONLINE: 'success', OFFLINE: 'danger', DEGRADED: 'warning' }[s] || 'info' }
function circuitBreakerType(s) { return { CLOSED: 'success', OPEN: 'danger', HALF_OPEN: 'warning' }[s] || 'success' }
function statusTagType(s) { return { PENDING: 'info', PREPROCESSING: 'warning', QUEUED: 'warning', DISPATCHED: 'warning', TRANSCRIBING: '', SUCCEEDED: 'success', FAILED: 'danger', CANCELED: 'info' }[s] || 'info' }
function statusLabel(s) { return { PENDING: '待处理', PREPROCESSING: '预处理', QUEUED: '排队中', DISPATCHED: '已分配', TRANSCRIBING: '转写中', SUCCEEDED: '已完成', FAILED: '失败', CANCELED: '已取消' }[s] || s }

async function handleProbe(row, level) {
  const tag = level === 'connect_only' ? 'connect' : 'speed'
  row._probing = tag
  try {
    const result = await probeServer(row.server_id, level)
    if (result.reachable) {
      ElMessage.success(`${row.server_id} 可达 (${result.probe_duration_ms?.toFixed(0) || '-'}ms)`)
    } else {
      ElMessage.error(`${row.server_id} 不可达: ${result.error || '连接失败'}`)
    }
    await fetchServers()
  } catch (e) {
    ElMessage.error(`探测失败: ${e.response?.data?.detail || e.message}`)
  } finally {
    row._probing = null
  }
}

// --- 添加 / 编辑 对话框 ---
const dialogVisible = ref(false)
const isEditing = ref(false)
const submitting = ref(false)
const formRef = ref(null)
const form = ref(defaultForm())

function defaultForm() {
  return { server_id: '', name: '', host: '', port: 10095, protocol_version: 'funasr-main', max_concurrency: 4 }
}

const formRules = {
  server_id: [{ required: true, message: '请输入节点ID', trigger: 'blur' }],
  host: [{ required: true, message: '请输入主机地址', trigger: 'blur' }],
  port: [{ required: true, message: '请输入端口', trigger: 'blur' }],
  protocol_version: [{ required: true, message: '请选择协议版本', trigger: 'change' }],
}

function openAddDialog() {
  isEditing.value = false
  form.value = defaultForm()
  dialogVisible.value = true
}

function openEditDialog(row) {
  isEditing.value = true
  form.value = {
    server_id: row.server_id,
    name: row.name || '',
    host: row.host,
    port: row.port,
    protocol_version: row.protocol_version,
    max_concurrency: row.max_concurrency,
  }
  dialogVisible.value = true
}

function resetForm() {
  formRef.value?.resetFields()
}

async function handleSubmit() {
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  submitting.value = true
  try {
    if (isEditing.value) {
      const { server_id, ...updates } = form.value
      await updateServer(server_id, updates)
      ElMessage.success('服务器已更新')
    } else {
      await registerServer(form.value)
      ElMessage.success('服务器已添加')
    }
    dialogVisible.value = false
    await fetchServers()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || e.message)
  } finally {
    submitting.value = false
  }
}

onMounted(() => { fetchServers(); fetchTasks(); fetchStats(); pollTimer = setInterval(() => { fetchServers(); fetchTasks(); fetchStats() }, 5000) })
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<style scoped>
.monitor-view { max-width: 1400px; }
.stats-row { margin-bottom: 16px; }
.stat-card { text-align: center; padding: 12px 0; }
.stat-icon { margin-bottom: 4px; }
.stat-value { font-size: 24px; font-weight: 700; line-height: 1.4; }
.stat-label { font-size: 12px; color: #909399; margin-top: 2px; }
.card-header { display: flex; align-items: center; justify-content: space-between; }
.mt-16 { margin-top: 16px; }
</style>
