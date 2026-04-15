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

export async function healthCheck() {
  const key = getApiKey()
  const headers = key ? { 'X-API-Key': key } : {}
  const { data } = await axios.get('/health', { headers, timeout: 10000 })
  return data
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
    const config = err.config
    if (err.response?.status === 401 && !_apiKeyPromptPending && !config?._retried) {
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
        if (value && config) {
          setApiKey(value)
          config._retried = true
          return api.request(config)
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

export async function listTasks(params = {}, config = {}) {
  const { data } = await api.get('/tasks', { params, ...config })
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

export async function listServers(config = {}) {
  const { data } = await api.get('/servers', config)
  return data
}

/**
 * Issue a fetch() request with the stored API Key. On 401, prompt the user
 * for a new key (mirroring the axios interceptor flow) and retry once.
 */
async function _authedFetch(url, init = {}) {
  const attempt = (key) => {
    const headers = { ...init.headers }
    if (key) headers['X-API-Key'] = key
    return fetch(url, { ...init, headers })
  }

  let resp = await attempt(getApiKey())

  if (resp.status === 401) {
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
        },
      )
      if (value) {
        setApiKey(value)
        resp = await attempt(value)
      }
    } catch {
      /* user cancelled prompt */
    }
  }
  return resp
}

/**
 * Parse an NDJSON stream from a fetch Response, calling onEvent for each line.
 * Returns a promise that resolves when the stream ends.
 */
async function _streamNdjson(url, { method = 'POST', body, onEvent, timeoutMs = 600000 }) {
  const headers = {}
  if (body) headers['Content-Type'] = 'application/json'

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)

  try {
    const resp = await _authedFetch(`/api/v1${url}`, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
      signal: controller.signal,
    })
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }))
      throw new Error(err.detail || `HTTP ${resp.status}`)
    }

    const reader = resp.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop()
      for (const line of lines) {
        const trimmed = line.trim()
        if (!trimmed) continue
        try { onEvent(JSON.parse(trimmed)) } catch { /* skip malformed */ }
      }
    }
    if (buffer.trim()) {
      try { onEvent(JSON.parse(buffer.trim())) } catch { /* skip */ }
    }
  } finally {
    clearTimeout(timer)
  }
}

export async function benchmarkServerStream(serverId, onEvent) {
  await _streamNdjson(`/servers/${serverId}/benchmark`, { onEvent })
}

export async function benchmarkAllServersStream(onEvent) {
  await _streamNdjson('/servers/benchmark', { onEvent })
}

export async function registerServer(serverData, onEvent = null) {
  if (serverData.run_benchmark && onEvent) {
    let serverResult = null
    let benchmarkError = null
    await _streamNdjson('/servers', {
      body: serverData,
      onEvent(event) {
        if (event.type === 'server_registered') serverResult = event.data
        if (event.type === 'benchmark_error') benchmarkError = event.error || 'benchmark failed'
        onEvent(event)
      },
    })
    return { _server: serverResult || {}, _benchmarkError: benchmarkError }
  }
  const timeout = serverData.run_benchmark ? 600000 : 30000
  const { data } = await api.post('/servers', serverData, { timeout })
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

export async function getSystemStats(config = {}) {
  const { data } = await api.get('/stats', config)
  return data
}

export async function getDiagnostics() {
  const { data } = await api.get('/diagnostics')
  return data
}
