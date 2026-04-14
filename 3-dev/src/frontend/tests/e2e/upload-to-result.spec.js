import path from 'node:path'

import { expect, test } from '@playwright/test'

import { createRunArtifacts, copyFile, writeJson, writeText } from './helpers/artifacts.js'
import { getAbsoluteFixturePaths, getFixtureBatchPath, loadFixtureBatch } from './helpers/fixture-batch.js'
import { evaluateTranscriptAgainstBaseline, getSemanticBaselinePath, loadSemanticBaseline } from './helpers/semantic-baseline.js'

const TERMINAL_STATUSES = new Set(['SUCCEEDED', 'FAILED', 'CANCELED'])
const timeoutByProfile = {
  smoke: 10 * 60 * 1000,
  'remote-standard': 20 * 60 * 1000,
  standard: 30 * 60 * 1000,
  full: 90 * 60 * 1000,
}

function buildDefaultServerId(host, port) {
  return `funasr-e2e-${host.replace(/[^a-zA-Z0-9]+/g, '-').replace(/^-+|-+$/g, '').toLowerCase()}-${port}`
}

async function ensureServerRegistered(request) {
  const configuredHost = process.env.ASR_E2E_SERVER_HOST
  const configuredPort = Number(process.env.ASR_E2E_SERVER_PORT || '10095')
  const configuredServerId = process.env.ASR_E2E_SERVER_ID
  const configuredProtocol = process.env.ASR_E2E_SERVER_PROTOCOL || 'funasr-main'
  const configuredName = process.env.ASR_E2E_SERVER_NAME
  const configuredConcurrency = Number(process.env.ASR_E2E_SERVER_MAX_CONCURRENCY || '4')

  const listResp = await request.get('/api/v1/servers')
  expect(listResp.ok()).toBeTruthy()
  const servers = await listResp.json()

  if (configuredServerId) {
    const matchedServer = servers.find((item) => item.server_id === configuredServerId)
    if (matchedServer) {
      return {
        serverId: matchedServer.server_id,
        host: matchedServer.host,
        port: matchedServer.port,
        reusedExistingServer: true,
      }
    }
  }

  if (configuredHost) {
    const matchedByEndpoint = servers.find((item) => (
      item.host === configuredHost && Number(item.port) === configuredPort
    ))
    if (matchedByEndpoint) {
      return {
        serverId: matchedByEndpoint.server_id,
        host: matchedByEndpoint.host,
        port: matchedByEndpoint.port,
        reusedExistingServer: true,
      }
    }
  }

  if (!configuredHost) {
    if (servers.length > 0) {
      const [firstServer] = servers
      return {
        serverId: firstServer.server_id,
        host: firstServer.host,
        port: firstServer.port,
        reusedExistingServer: true,
      }
    }

    throw new Error(
      'No ASR server registered. Set ASR_E2E_SERVER_HOST/ASR_E2E_SERVER_PORT or pre-register a server before running the browser E2E test.',
    )
  }

  const serverId = configuredServerId || buildDefaultServerId(configuredHost, configuredPort)

  const createResp = await request.post('/api/v1/servers', {
    data: {
      server_id: serverId,
      name: configuredName || `FunASR ${configuredHost}:${configuredPort}`,
      host: configuredHost,
      port: configuredPort,
      protocol_version: configuredProtocol,
      max_concurrency: configuredConcurrency,
    },
  })
  expect([201, 409]).toContain(createResp.status())
  return {
    serverId,
    host: configuredHost,
    port: configuredPort,
    reusedExistingServer: createResp.status() === 409,
  }
}

async function waitForTerminalTasks(request, taskIds, timeoutMs = 10 * 60 * 1000) {
  const startedAt = Date.now()
  const pollHistory = []

  while (Date.now() - startedAt < timeoutMs) {
    const states = []
    let allDone = true

    for (const taskId of taskIds) {
      const response = await request.get(`/api/v1/tasks/${taskId}`)
      expect(response.ok()).toBeTruthy()
      const payload = await response.json()
      states.push(payload)
      if (!TERMINAL_STATUSES.has(payload.status)) {
        allDone = false
      }
    }

    pollHistory.push({
      polled_at: new Date().toISOString(),
      tasks: states.map((item) => ({
        task_id: item.task_id,
        status: item.status,
        progress: item.progress,
        eta_seconds: item.eta_seconds,
        assigned_server_id: item.assigned_server_id,
        error_message: item.error_message,
        started_at: item.started_at,
        completed_at: item.completed_at,
      })),
    })

    if (allDone) {
      return { states, pollHistory }
    }

    await new Promise((resolve) => setTimeout(resolve, 5000))
  }

  throw new Error(`Timed out waiting for terminal task states: ${taskIds.join(', ')}`)
}

test('@smoke upload to result flow', async ({ page, request, baseURL }) => {
  const profile = process.env.ASR_E2E_PROFILE || 'smoke'
  test.setTimeout(timeoutByProfile[profile] || timeoutByProfile.smoke)
  const batch = loadFixtureBatch(profile)
  const semanticBaseline = loadSemanticBaseline()
  const artifacts = createRunArtifacts(profile)
  copyFile(getFixtureBatchPath(profile), path.join(artifacts.runDir, 'fixture-batch.json'))
  if (semanticBaseline.exists) {
    copyFile(getSemanticBaselinePath(), path.join(artifacts.runDir, 'semantic-baseline.json'))
  }
  const sourceFileByFileId = new Map()

  const healthResponse = await request.get('/health')
  expect(healthResponse.ok()).toBeTruthy()
  const serverRegistration = await ensureServerRegistered(request)

  await page.goto(`${baseURL}/upload`, { waitUntil: 'networkidle' })

  await expect(page.getByTestId('upload-dropzone')).toBeVisible()

  await page.locator('input[type="file"]').setInputFiles(getAbsoluteFixturePaths(batch))

  await expect(page.getByTestId('pending-files-table')).toBeVisible()
  await page.screenshot({ path: path.join(artifacts.screenshotsDir, 'upload-pending.png'), fullPage: true })

  let uploadResponseIndex = 0
  const uploadResponseHandler = async (response) => {
    if (!response.url().includes('/api/v1/files/upload') || response.request().method() !== 'POST') {
      return
    }

    const uploaded = await response.json()
    const sourceFile = batch.files[uploadResponseIndex]
    uploadResponseIndex += 1

    if (sourceFile?.name && uploaded?.file_id) {
      sourceFileByFileId.set(uploaded.file_id, sourceFile.name)
    }
  }
  page.on('response', uploadResponseHandler)

  const createTaskResponse = page.waitForResponse((response) => (
    response.url().includes('/api/v1/tasks') && response.request().method() === 'POST'
  ))

  await page.getByTestId('submit-transcribe').click()

  const taskResponse = await createTaskResponse
  expect(taskResponse.ok()).toBeTruthy()
  const createdTasks = await taskResponse.json()
  expect(createdTasks).toHaveLength(batch.files.length)

  await expect(page.getByTestId('created-tasks-table')).toBeVisible()
  await page.screenshot({ path: path.join(artifacts.screenshotsDir, 'upload-created-tasks.png'), fullPage: true })
  page.off('response', uploadResponseHandler)

  const taskIds = createdTasks.map((item) => item.task_id)

  await page.goto(`${baseURL}/tasks`, { waitUntil: 'networkidle' })
  await expect(page.getByTestId('task-list-table')).toBeVisible()

  const { states, pollHistory } = await waitForTerminalTasks(request, taskIds, timeoutByProfile[profile] || timeoutByProfile.smoke)
  await page.reload({ waitUntil: 'networkidle' })
  await page.screenshot({ path: path.join(artifacts.screenshotsDir, 'tasks-final.png'), fullPage: true })

  const results = []
  for (const [index, task] of states.entries()) {
    const entry = {
      task_id: task.task_id,
      source_file: sourceFileByFileId.get(task.file_id) || batch.files[index]?.name || task.file_id,
      file_id: task.file_id,
      status: task.status,
      progress: task.progress,
      eta_seconds: task.eta_seconds,
      assigned_server_id: task.assigned_server_id,
      error_message: task.error_message,
      started_at: task.started_at,
      completed_at: task.completed_at,
    }

    if (task.status === 'SUCCEEDED') {
      const resultResp = await request.get(`/api/v1/tasks/${task.task_id}/result?format=txt`)
      expect(resultResp.ok()).toBeTruthy()
      const text = await resultResp.text()
      expect(text.trim().length).toBeGreaterThan(0)
      const resultPath = path.join(artifacts.resultsDir, `${task.task_id}.txt`)
      writeText(resultPath, text)
      entry.text_length = text.trim().length
      entry.non_empty = true
      entry.contains_cjk = /[\u4e00-\u9fff]/.test(text)
      entry.preview = text.slice(0, 200)
      entry.result_file = resultPath
      entry.semantic_baseline = evaluateTranscriptAgainstBaseline(entry.source_file, text, semanticBaseline)
      if (entry.semantic_baseline.status === 'configured') {
        expect(entry.semantic_baseline.passed).toBeTruthy()
      }
    }
    results.push(entry)
  }

  const summary = {
    run_timestamp: artifacts.timestamp,
    profile,
    platform: process.platform,
    node_version: process.version,
    base_url: baseURL,
    asr_server: serverRegistration,
    semantic_baseline_path: semanticBaseline.baselinePath,
    selected_files: batch.files.map((item) => item.name),
    task_ids: taskIds,
    file_task_pairs: results.map((item) => ({ source_file: item.source_file, file_id: item.file_id, task_id: item.task_id })),
    success_count: results.filter((item) => item.status === 'SUCCEEDED').length,
    failure_count: results.filter((item) => item.status !== 'SUCCEEDED').length,
    results,
    poll_history: pollHistory,
  }

  writeJson(path.join(artifacts.runDir, 'run-summary.json'), summary)
  writeText(
    path.join(artifacts.runDir, 'run-summary.md'),
    [
      'FunASR E2E 测试报告',
      `执行时间: ${artifacts.timestamp}`,
      `测试方案: ${profile}`,
      '',
      ...results.map((item) => `- ${item.source_file}: ${item.status} | text_length=${item.text_length || 0} | server=${item.assigned_server_id || '-'}`),
      '',
      `总计: ${summary.success_count}/${results.length} 通过 | ${summary.failure_count} 失败`,
    ].join('\n'),
  )

  expect(summary.failure_count).toBe(0)
})