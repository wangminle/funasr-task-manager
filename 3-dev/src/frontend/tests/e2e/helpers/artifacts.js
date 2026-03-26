import fs from 'node:fs'
import path from 'node:path'

import { getWorkspaceRoot } from './fixture-batch.js'

function pad(value) {
  return String(value).padStart(2, '0')
}

export function createRunArtifacts(profile) {
  const now = new Date()
  const timestamp = [
    now.getFullYear(),
    pad(now.getMonth() + 1),
    pad(now.getDate()),
  ].join('') + '-' + [pad(now.getHours()), pad(now.getMinutes()), pad(now.getSeconds())].join('')

  const runDir = path.join(getWorkspaceRoot(), '7-data', 'outputs', 'e2e', timestamp)
  const screenshotsDir = path.join(runDir, 'screenshots')
  const resultsDir = path.join(runDir, 'results')

  fs.mkdirSync(screenshotsDir, { recursive: true })
  fs.mkdirSync(resultsDir, { recursive: true })

  return {
    profile,
    timestamp,
    runDir,
    screenshotsDir,
    resultsDir,
  }
}

export function writeJson(filePath, value) {
  fs.writeFileSync(filePath, JSON.stringify(value, null, 2), 'utf-8')
}

export function writeText(filePath, value) {
  fs.writeFileSync(filePath, value, 'utf-8')
}

export function copyFile(source, target) {
  fs.copyFileSync(source, target)
}