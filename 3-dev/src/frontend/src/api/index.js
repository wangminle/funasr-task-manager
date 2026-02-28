import axios from 'axios'

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

api.interceptors.response.use(
  resp => resp,
  err => {
    if (err.response?.status === 401) {
      const key = window.prompt('请输入 API Key：', getApiKey())
      if (key) {
        setApiKey(key)
        return api.request(err.config)
      }
    }
    return Promise.reject(err)
  }
)

export async function uploadFile(file) {
  const formData = new FormData()
  formData.append('file', file)
  const { data } = await api.post('/files/upload', formData, { headers: { 'Content-Type': 'multipart/form-data' } })
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

export async function deleteServer(serverId) {
  await api.delete(`/servers/${serverId}`)
}
