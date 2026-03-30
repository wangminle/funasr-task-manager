import axios from 'axios'
import { ElMessageBox } from 'element-plus'

const api = axios.create({ baseURL: '/api/v1', timeout: 30000 })

const API_KEY_STORAGE = 'asr_api_key'

export function setApiKey(key) {
  localStorage.setItem(API_KEY_STORAGE, key)
}

export function getApiKey() {
  return localStorage.getItem(API_KEY_STORAGE) || ''
}

api.interceptors.request.use(config => {
  const key = getApiKey()
  if (key) config.headers['X-API-Key'] = key
  return config
})

let _apiKeyPromptPending = false

api.interceptors.response.use(
  resp => resp,
  async err => {
    if (err.response?.status === 401 && !_apiKeyPromptPending) {
      _apiKeyPromptPending = true
      try {
        const { value } = await ElMessageBox.prompt(
          '当前请求需要认证，请输入有效的 API Key。',
          '认证失败',
          {
            confirmButtonText: '确定',
            cancelButtonText: '取消',
            inputValue: getApiKey(),
            inputPlaceholder: '请输入 API Key',
            type: 'warning',
          }
        )
        if (value) {
          setApiKey(value)
          return api.request(err.config)
        }
      } catch {
        // user cancelled
      } finally {
        _apiKeyPromptPending = false
      }
    }
    return Promise.reject(err)
  }
)

export async function uploadFile(file, onProgress) {
  const formData = new FormData()
  formData.append('file', file)
  const config = {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 600000,
  }
  if (onProgress) {
    config.onUploadProgress = (e) => {
      const pct = e.total ? Math.round((e.loaded / e.total) * 100) : 0
      onProgress(pct)
    }
  }
  const { data } = await api.post('/files/upload', formData, config)
  return data
}

export async function getFileMetadata(fileId) {
  const { data } = await api.get(`/files/${fileId}`)
  return data
}

export async function createTasks(items, callback = null) {
  const body = { items }
  if (callback) body.callback = callback
  const { data } = await api.post('/tasks', body)
  return data
}

export async function listTasks(params = {}) {
  const { data } = await api.get('/tasks', { params })
  return data
}

export async function getTask(taskId) {
  const { data } = await api.get(`/tasks/${taskId}`)
  return data
}

export async function cancelTask(taskId) {
  const { data } = await api.post(`/tasks/${taskId}/cancel`)
  return data
}

export async function deleteAllTasks(status = null) {
  const params = {}
  if (status) params.status = status
  const { data } = await api.delete('/tasks', { params })
  return data
}

export async function getTaskResult(taskId, format = 'json') {
  const { data } = await api.get(`/tasks/${taskId}/result`, { params: { format } })
  return data
}

export async function listServers() {
  const { data } = await api.get('/servers')
  return data
}

export async function registerServer(serverData) {
  const { data } = await api.post('/servers', serverData)
  return data
}

export async function updateServer(serverId, data) {
  const { data: resp } = await api.patch(`/servers/${serverId}`, data)
  return resp
}

export async function deleteServer(serverId) {
  await api.delete(`/servers/${serverId}`)
}

export async function probeServer(serverId, level = 'connect_only') {
  const { data } = await api.post(`/servers/${serverId}/probe`, null, { params: { level } })
  return data
}

export async function getSystemStats() {
  const { data } = await api.get('/stats')
  return data
}

export async function getDiagnostics() {
  const { data } = await api.get('/diagnostics')
  return data
}
