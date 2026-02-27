<template>
  <div class="monitor-view">
    <el-row :gutter="16" class="stats-row">
      <el-col :span="6">
        <el-card shadow="never" class="stat-card"><div class="stat-value" style="color:#67c23a;">{{ onlineCount }}</div><div class="stat-label">在线节点</div></el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never" class="stat-card"><div class="stat-value" style="color:#f56c6c;">{{ offlineCount }}</div><div class="stat-label">离线节点</div></el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never" class="stat-card"><div class="stat-value" style="color:#e6a23c;">{{ queueDepth }}</div><div class="stat-label">队列深度</div></el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="never" class="stat-card"><div class="stat-value" style="color:#409eff;">{{ activeTasks }}</div><div class="stat-label">活跃任务</div></el-card>
      </el-col>
    </el-row>

    <el-card shadow="never" class="mt-16">
      <template #header>
        <div class="card-header">
          <span>ASR 服务器节点</span>
          <el-button @click="fetchServers" :icon="Refresh">刷新</el-button>
        </div>
      </template>
      <el-table :data="servers" stripe v-loading="loading">
        <el-table-column prop="server_id" label="节点ID" width="160" />
        <el-table-column prop="name" label="名称" width="140">
          <template #default="{ row }">{{ row.name || '-' }}</template>
        </el-table-column>
        <el-table-column label="地址" width="180">
          <template #default="{ row }">{{ row.host }}:{{ row.port }}</template>
        </el-table-column>
        <el-table-column prop="protocol_version" label="协议" width="80" />
        <el-table-column label="并发" width="80">
          <template #default="{ row }">{{ row.max_concurrency }}</template>
        </el-table-column>
        <el-table-column label="RTF" width="80">
          <template #default="{ row }">{{ row.rtf_baseline ? row.rtf_baseline.toFixed(2) : '-' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="serverStatusType(row.status)" effect="dark" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="断路器" width="110">
          <template #default="{ row }">
            <el-tag :type="circuitBreakerType(row.circuit_breaker)" size="small">{{ row.circuit_breaker || 'CLOSED' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="最后心跳" width="170">
          <template #default="{ row }">{{ row.last_heartbeat ? new Date(row.last_heartbeat).toLocaleString('zh-CN') : '从未' }}</template>
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
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { listServers, listTasks } from '../api'

const servers = ref([])
const tasks = ref([])
const loading = ref(false)
let pollTimer = null

const onlineCount = computed(() => servers.value.filter(s => s.status === 'ONLINE').length)
const offlineCount = computed(() => servers.value.filter(s => s.status !== 'ONLINE').length)
const totalSlots = computed(() => servers.value.reduce((sum, s) => sum + s.max_concurrency, 0))
const activeTasks = computed(() => tasks.value.filter(t => ['PREPROCESSING', 'DISPATCHED', 'TRANSCRIBING'].includes(t.status)).length)
const queueDepth = computed(() => tasks.value.filter(t => ['PENDING', 'QUEUED'].includes(t.status)).length)
const taskStatusCounts = computed(() => {
  const counts = {}
  for (const t of tasks.value) { counts[t.status] = (counts[t.status] || 0) + 1 }
  return counts
})

async function fetchServers() {
  loading.value = true
  try { servers.value = await listServers() } catch { ElMessage.error('获取服务器列表失败') } finally { loading.value = false }
}
async function fetchTasks() {
  try { const data = await listTasks({ page: 1, page_size: 200 }); tasks.value = data.items } catch {}
}
function serverStatusType(s) { return { ONLINE: 'success', OFFLINE: 'danger', DEGRADED: 'warning' }[s] || 'info' }
function circuitBreakerType(s) { return { CLOSED: 'success', OPEN: 'danger', HALF_OPEN: 'warning' }[s] || 'success' }
function statusTagType(s) { return { PENDING: 'info', PREPROCESSING: 'warning', QUEUED: 'warning', DISPATCHED: 'warning', TRANSCRIBING: '', SUCCEEDED: 'success', FAILED: 'danger', CANCELED: 'info' }[s] || 'info' }
function statusLabel(s) { return { PENDING: '待处理', PREPROCESSING: '预处理', QUEUED: '排队中', DISPATCHED: '已分配', TRANSCRIBING: '转写中', SUCCEEDED: '已完成', FAILED: '失败', CANCELED: '已取消' }[s] || s }

onMounted(() => { fetchServers(); fetchTasks(); pollTimer = setInterval(() => { fetchServers(); fetchTasks() }, 5000) })
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer) })
</script>

<style scoped>
.monitor-view { max-width: 1100px; margin: 0 auto; }
.stats-row { margin-bottom: 16px; }
.stat-card { text-align: center; }
.stat-value { font-size: 32px; font-weight: 700; }
.stat-label { font-size: 13px; color: #909399; margin-top: 4px; }
.card-header { display: flex; align-items: center; justify-content: space-between; }
.mt-16 { margin-top: 16px; }
</style>
