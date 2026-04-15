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
          <span>实时趋势监控</span>
          <el-tag type="info" size="small">最近 {{ MAX_HISTORY_POINTS }} 个采样点</el-tag>
        </div>
      </template>
      <el-row :gutter="16">
        <el-col :span="12">
          <div class="chart-title">队列深度 & 活跃任务</div>
          <v-chart :option="queueChartOption" autoresize style="height: 240px;" />
        </el-col>
        <el-col :span="12">
          <div class="chart-title">今日任务完成 & 失败</div>
          <v-chart :option="taskChartOption" autoresize style="height: 240px;" />
        </el-col>
      </el-row>
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
            <el-descriptions-item label="平均 RTF">{{ sysStats.avg_rtf != null ? sysStats.avg_rtf.toFixed(3) : '-' }}</el-descriptions-item>
            <el-descriptions-item label="数据刷新间隔">5 秒</el-descriptions-item>
          </el-descriptions>
        </el-col>
      </el-row>
    </el-card>

    <el-card shadow="never" class="mt-16">
      <template #header>
        <div class="card-header">
          <span>系统诊断</span>
          <el-button size="small" @click="fetchDiagnostics" :loading="diagLoading" :icon="Refresh">刷新诊断</el-button>
        </div>
      </template>
      <el-alert v-if="diagError" :title="diagError" type="warning" :closable="false" style="margin-bottom: 12px;" />
      <el-table v-else :data="diagChecks" stripe v-loading="diagLoading" empty-text="点击「刷新诊断」获取数据">
        <el-table-column prop="name" label="检查项" width="200" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="diagLevelType(row.level)" size="small" effect="dark">
              {{ diagLevelIcon(row.level) }} {{ row.level }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="detail" label="说明" />
      </el-table>
      <el-alert
        v-if="diagHasBlockingErrors"
        title="存在阻断性问题，请立即处理！"
        type="error"
        :closable="false"
        show-icon
        style="margin-top: 12px;"
      />
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
        <el-form-item v-if="!isEditing" label="运行测速">
          <el-switch v-model="form.run_benchmark" active-text="添加后自动 Benchmark" />
          <div style="font-size: 12px; color: #909399; margin-top: 4px;">
            开启后将在注册成功时执行一次完整性能基准测试，耗时较长
          </div>
        </el-form-item>
      </el-form>
      <div v-if="benchmarkProgress.length > 0" style="margin-top: 12px; max-height: 320px; overflow-y: auto;">
        <el-divider content-position="left">Benchmark 进度</el-divider>
        <el-timeline>
          <el-timeline-item
            v-for="(item, idx) in benchmarkProgress"
            :key="idx"
            :type="item.tagType"
            :timestamp="item.time"
            placement="top"
            :hollow="idx < benchmarkProgress.length - 1"
          >
            {{ item.message }}
          </el-timeline-item>
        </el-timeline>
      </div>
      <template #footer>
        <el-button @click="dialogVisible = false" :disabled="submitting">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="handleSubmit">{{ isEditing ? '保存' : '添加' }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, markRaw } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import { Refresh, Plus, Monitor as MonitorIcon, WarningFilled, List, Timer, SuccessFilled, TrendCharts } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { listServers, listTasks, probeServer, registerServer, updateServer, getSystemStats, getDiagnostics } from '../api'

use([CanvasRenderer, LineChart, GridComponent, TooltipComponent, LegendComponent])

const MAX_HISTORY_POINTS = 60

const servers = ref([])
const tasks = ref([])
const sysStats = ref({
  server_total: 0, server_online: 0, slots_total: 0, slots_used: 0,
  queue_depth: 0, tasks_today_completed: 0, tasks_today_failed: 0,
  success_rate_24h: 100, avg_rtf: null,
})
const loading = ref(false)
let pollTimer = null
let pollAbortController = null

const historyLabels = ref([])
const historyQueueDepth = ref([])
const historySlotsUsed = ref([])
const historyCompleted = ref([])
const historyFailed = ref([])

function recordHistory() {
  const now = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  const s = sysStats.value
  historyLabels.value.push(now)
  historyQueueDepth.value.push(s.queue_depth)
  historySlotsUsed.value.push(s.slots_used)
  historyCompleted.value.push(s.tasks_today_completed)
  historyFailed.value.push(s.tasks_today_failed)
  if (historyLabels.value.length > MAX_HISTORY_POINTS) {
    historyLabels.value.shift()
    historyQueueDepth.value.shift()
    historySlotsUsed.value.shift()
    historyCompleted.value.shift()
    historyFailed.value.shift()
  }
}

const chartBaseOption = {
  tooltip: { trigger: 'axis' },
  grid: { left: 40, right: 16, top: 36, bottom: 24 },
}

const queueChartOption = computed(() => ({
  ...chartBaseOption,
  legend: { data: ['队列深度', '占用槽位'] },
  xAxis: { type: 'category', data: historyLabels.value, axisLabel: { fontSize: 10 } },
  yAxis: { type: 'value', minInterval: 1 },
  series: [
    { name: '队列深度', type: 'line', smooth: true, data: historyQueueDepth.value, itemStyle: { color: '#e6a23c' }, areaStyle: { color: 'rgba(230,162,60,0.1)' } },
    { name: '占用槽位', type: 'line', smooth: true, data: historySlotsUsed.value, itemStyle: { color: '#409eff' }, areaStyle: { color: 'rgba(64,158,255,0.1)' } },
  ],
}))

const taskChartOption = computed(() => ({
  ...chartBaseOption,
  legend: { data: ['今日完成', '今日失败'] },
  xAxis: { type: 'category', data: historyLabels.value, axisLabel: { fontSize: 10 } },
  yAxis: { type: 'value', minInterval: 1 },
  series: [
    { name: '今日完成', type: 'line', smooth: true, data: historyCompleted.value, itemStyle: { color: '#67c23a' }, areaStyle: { color: 'rgba(103,194,58,0.1)' } },
    { name: '今日失败', type: 'line', smooth: true, data: historyFailed.value, itemStyle: { color: '#f56c6c' }, areaStyle: { color: 'rgba(245,108,108,0.1)' } },
  ],
}))

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

async function fetchServers(signal) {
  loading.value = true
  try { servers.value = await listServers({ signal }) } catch (err) { if (err?.name !== 'CanceledError') ElMessage.error('获取服务器列表失败') } finally { loading.value = false }
}
async function fetchTasks(signal) {
  try { const data = await listTasks({ page: 1, page_size: 200 }, { signal }); tasks.value = data.items } catch (err) { if (err?.name !== 'CanceledError') console.warn('获取任务列表失败', err) }
}
async function fetchStats(signal) {
  try {
    sysStats.value = await getSystemStats({ signal })
    recordHistory()
  } catch (err) { if (err?.name !== 'CanceledError') console.warn('获取统计数据失败', err) }
}
const diagChecks = ref([])
const diagHasBlockingErrors = ref(false)
const diagLoading = ref(false)
const diagError = ref('')

async function fetchDiagnostics() {
  diagLoading.value = true
  diagError.value = ''
  try {
    const report = await getDiagnostics()
    diagChecks.value = report.checks || []
    diagHasBlockingErrors.value = report.has_blocking_errors || false
  } catch (err) {
    const status = err.response?.status
    if (status === 401 || status === 403) {
      diagError.value = '诊断接口需要管理员权限，请在设置页配置 Admin API Key'
    } else {
      diagError.value = `获取诊断数据失败: ${err.response?.data?.detail || err.message}`
    }
  } finally {
    diagLoading.value = false
  }
}

function diagLevelType(level) {
  return { ok: 'success', warning: 'warning', error: 'danger' }[level] || 'info'
}
function diagLevelIcon(level) {
  return { ok: '✓', warning: '⚠', error: '✗' }[level] || '?'
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
const benchmarkProgress = ref([])

function defaultForm() {
  return { server_id: '', name: '', host: '', port: 10095, protocol_version: 'funasr-main', max_concurrency: 4, run_benchmark: false }
}

function addBenchmarkProgress(message, tagType = '') {
  benchmarkProgress.value.push({
    message,
    tagType,
    time: new Date().toLocaleTimeString('zh-CN'),
  })
}

function handleBenchmarkEvent(event) {
  const t = event.type
  if (t === 'server_registered') {
    addBenchmarkProgress('服务器注册成功', 'success')
  } else if (t === 'benchmark_start') {
    addBenchmarkProgress(`Benchmark 开始 (样本: ${(event.samples || []).join(', ')})`, 'primary')
  } else if (t === 'phase_start') {
    addBenchmarkProgress(`Phase ${event.phase}: ${event.description || ''}`, 'primary')
  } else if (t === 'phase_progress') {
    addBenchmarkProgress(`采样 ${event.rep}/${event.total_reps}: RTF=${event.rtf}`)
  } else if (t === 'phase_complete') {
    addBenchmarkProgress(`Phase ${event.phase} 完成: single_rtf=${event.single_rtf}`, 'success')
  } else if (t === 'gradient_start') {
    addBenchmarkProgress(`梯度 N=${event.concurrency} (${event.level_index}/${event.total_levels})...`)
  } else if (t === 'gradient_complete') {
    addBenchmarkProgress(`N=${event.concurrency}: throughput_rtf=${event.throughput_rtf}, wall=${event.wall_clock_sec}s`, 'success')
  } else if (t === 'gradient_error') {
    addBenchmarkProgress(`N=${event.concurrency}: ${event.error || '失败'}`, 'warning')
  } else if (t === 'benchmark_complete') {
    addBenchmarkProgress(`完成: 推荐并发=${event.recommended_concurrency}, throughput_rtf=${event.throughput_rtf}`, 'success')
  } else if (t === 'benchmark_result') {
    addBenchmarkProgress('结果已保存到数据库', 'success')
  } else if (t === 'benchmark_error') {
    addBenchmarkProgress(`Benchmark 错误: ${event.error || ''}`, 'danger')
  } else if (t === 'ssl_fallback') {
    addBenchmarkProgress('WSS 连接失败，回退到 WS 重试...', 'warning')
  }
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
  benchmarkProgress.value = []
}

async function handleSubmit() {
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return
  submitting.value = true
  benchmarkProgress.value = []
  try {
    if (isEditing.value) {
      const { server_id, run_benchmark, ...updates } = form.value
      await updateServer(server_id, updates)
      ElMessage.success('服务器已更新')
      dialogVisible.value = false
    } else if (form.value.run_benchmark) {
      const result = await registerServer(form.value, handleBenchmarkEvent)
      if (result._benchmarkError) {
        ElMessage.warning(`服务器已添加，但 Benchmark 失败: ${result._benchmarkError}`)
      } else {
        ElMessage.success('服务器已添加，Benchmark 完成')
      }
      dialogVisible.value = false
    } else {
      await registerServer(form.value)
      ElMessage.success('服务器已添加')
      dialogVisible.value = false
    }
    await fetchServers()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || e.message)
  } finally {
    submitting.value = false
  }
}

function schedulePoll() {
  pollTimer = setTimeout(async () => {
    if (pollAbortController) pollAbortController.abort()
    pollAbortController = new AbortController()
    const { signal } = pollAbortController
    await fetchServers(signal)
    await fetchTasks(signal)
    await fetchStats(signal)
    schedulePoll()
  }, 5000)
}

onMounted(() => { fetchServers(); fetchTasks(); fetchStats(); fetchDiagnostics(); schedulePoll() })
onUnmounted(() => {
  if (pollTimer) clearTimeout(pollTimer)
  if (pollAbortController) pollAbortController.abort()
})
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
.chart-title { font-size: 13px; font-weight: 600; color: #606266; margin-bottom: 4px; }
</style>
