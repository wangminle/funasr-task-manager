<template>
  <div class="settings-view">
    <el-row :gutter="16">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <el-icon :size="18"><Key /></el-icon>
              <span>API 认证</span>
            </div>
          </template>
          <el-form label-width="100px">
            <el-form-item label="API Key">
              <el-input v-model="apiKey" :type="showKey ? 'text' : 'password'" placeholder="输入 API Key 以启用认证请求">
                <template #append>
                  <el-button :icon="showKey ? Hide : View" @click="showKey = !showKey" />
                </template>
              </el-input>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" @click="saveApiKey">保存</el-button>
              <el-button @click="clearApiKey">清除</el-button>
            </el-form-item>
          </el-form>
        </el-card>

        <el-card shadow="never" class="mt-16">
          <template #header>
            <div class="card-header">
              <el-icon :size="18"><InfoFilled /></el-icon>
              <span>系统信息</span>
            </div>
          </template>
          <el-descriptions :column="1" border>
            <el-descriptions-item label="后端版本">{{ sysInfo.version || '-' }}</el-descriptions-item>
            <el-descriptions-item label="前端版本">v{{ appVersion }}</el-descriptions-item>
            <el-descriptions-item label="数据库类型">{{ sysInfo.database || '-' }}</el-descriptions-item>
            <el-descriptions-item label="认证状态">
              <el-tag :type="sysInfo.auth_enabled ? 'success' : 'warning'" size="small">
                {{ sysInfo.auth_enabled ? '已启用' : '未启用' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="运行时长">{{ sysInfo.uptime || '-' }}</el-descriptions-item>
          </el-descriptions>
        </el-card>
      </el-col>

      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            <div class="card-header">
              <el-icon :size="18"><Bell /></el-icon>
              <span>告警中心</span>
              <el-tag type="info" size="small" style="margin-left: auto;">基于统计阈值</el-tag>
            </div>
          </template>
          <el-alert title="以下告警由前端根据系统统计指标自动生成，非 Alertmanager 推送。" type="info" :closable="false" style="margin-bottom: 12px;" />
          <div v-if="alerts.length === 0" class="empty-alerts">
            <el-empty description="暂无告警" :image-size="80" />
          </div>
          <el-timeline v-else>
            <el-timeline-item
              v-for="(alert, idx) in alerts"
              :key="idx"
              :type="alertSeverityType(alert.severity)"
              :timestamp="alert.time"
              placement="top"
            >
              <el-card shadow="never" class="alert-card">
                <div class="alert-header">
                  <el-tag :type="alertSeverityType(alert.severity)" size="small">{{ alert.severity }}</el-tag>
                  <span class="alert-name">{{ alert.name }}</span>
                </div>
                <div class="alert-desc">{{ alert.description }}</div>
              </el-card>
            </el-timeline-item>
          </el-timeline>
        </el-card>

        <el-card shadow="never" class="mt-16">
          <template #header>
            <div class="card-header">
              <el-icon :size="18"><User /></el-icon>
              <span>快捷操作</span>
            </div>
          </template>
          <div class="quick-actions">
            <el-button @click="testConnection" :loading="testing">测试后端连接</el-button>
            <el-button type="danger" plain @click="confirmClearAllTasks">清空已完成任务</el-button>
          </div>
          <div v-if="connectionResult" class="connection-result mt-16">
            <el-alert :title="connectionResult.title" :type="connectionResult.type" :closable="false" show-icon />
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Key, View, Hide, InfoFilled, Bell, User } from '@element-plus/icons-vue'
import { getApiKey, setApiKey, getSystemStats, deleteAllTasks } from '../api'
import axios from 'axios'

const appVersion = __APP_VERSION__

const apiKey = ref(getApiKey())
const showKey = ref(false)

const sysInfo = ref({ version: '', database: '', auth_enabled: false, uptime: '' })
const alerts = ref([])

const testing = ref(false)
const connectionResult = ref(null)

function saveApiKey() {
  setApiKey(apiKey.value.trim())
  ElMessage.success('API Key 已保存')
}

function clearApiKey() {
  apiKey.value = ''
  setApiKey('')
  ElMessage.info('API Key 已清除')
}

async function fetchSystemInfo() {
  try {
    const resp = await axios.get('/health')
    const data = resp.data
    sysInfo.value = {
      version: data.version || '-',
      database: data.database_type || '-',
      auth_enabled: data.auth_enabled ?? false,
      uptime: data.uptime || '-',
    }
  } catch (err) {
    console.warn('获取系统信息失败', err)
  }
}

async function fetchAlerts() {
  try {
    const stats = await getSystemStats()
    const now = new Date().toLocaleString('zh-CN')
    const generated = []
    if (stats.success_rate_24h < 80) {
      generated.push({ severity: 'critical', name: '成功率过低', description: `24小时成功率仅 ${stats.success_rate_24h}%，低于 80% 阈值`, time: now })
    } else if (stats.success_rate_24h < 95) {
      generated.push({ severity: 'warning', name: '成功率偏低', description: `24小时成功率 ${stats.success_rate_24h}%，低于 95% 阈值`, time: now })
    }
    if (stats.queue_depth > 50) {
      generated.push({ severity: 'warning', name: '队列积压', description: `当前队列深度 ${stats.queue_depth}，建议扩容`, time: now })
    }
    if (stats.server_online === 0 && stats.server_total > 0) {
      generated.push({ severity: 'critical', name: '全部节点离线', description: `${stats.server_total} 个节点全部离线`, time: now })
    }
    if (stats.tasks_today_failed > 10) {
      generated.push({ severity: 'warning', name: '今日失败较多', description: `今日已失败 ${stats.tasks_today_failed} 个任务`, time: now })
    }
    if (generated.length === 0) {
      generated.push({ severity: 'info', name: '系统正常', description: '所有指标正常，无告警', time: now })
    }
    alerts.value = generated
  } catch (err) {
    console.warn('获取告警数据失败', err)
  }
}

function alertSeverityType(severity) {
  return { critical: 'danger', warning: 'warning', info: 'success' }[severity] || 'info'
}

async function testConnection() {
  testing.value = true
  connectionResult.value = null
  try {
    const resp = await axios.get('/health')
    if (resp.status === 200) {
      connectionResult.value = { title: '后端连接正常', type: 'success' }
    } else {
      connectionResult.value = { title: `后端响应异常: HTTP ${resp.status}`, type: 'warning' }
    }
  } catch (e) {
    connectionResult.value = { title: `连接失败: ${e.message}`, type: 'error' }
  } finally {
    testing.value = false
  }
}

async function confirmClearAllTasks() {
  try {
    await ElMessageBox.confirm('确定要删除所有已完成(SUCCEEDED)的任务记录吗？此操作不可撤销。', '确认删除', { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' })
    const result = await deleteAllTasks('SUCCEEDED')
    ElMessage.success(`已删除 ${result.deleted ?? 0} 条记录`)
  } catch {
    // user cancelled
  }
}

onMounted(() => {
  fetchSystemInfo()
  fetchAlerts()
})
</script>

<style scoped>
.settings-view { max-width: 1200px; }
.card-header { display: flex; align-items: center; gap: 8px; }
.mt-16 { margin-top: 16px; }
.empty-alerts { padding: 16px 0; }
.alert-card { margin: 0; }
.alert-header { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.alert-name { font-weight: 600; font-size: 14px; }
.alert-desc { font-size: 13px; color: #606266; }
.quick-actions { display: flex; gap: 12px; flex-wrap: wrap; }
.connection-result { margin-top: 12px; }
</style>
