import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const workspaceRoot = path.resolve(__dirname, '..', '..', '..', '..', '..', '..')

export function getWorkspaceRoot() {
  return workspaceRoot
}

export function getFixtureBatchPath(profile = 'smoke') {
  return path.join(getWorkspaceRoot(), '4-tests', 'batch-testing', 'outputs', 'e2e', 'fixture-batches', `${profile}.json`)
}

export function loadFixtureBatch(profile = 'smoke') {
  const fixtureBatchPath = getFixtureBatchPath(profile)
  if (!fs.existsSync(fixtureBatchPath)) {
    throw new Error(`Fixture batch not found: ${fixtureBatchPath}. Run the prepare script first.`)
  }

  try {
    return JSON.parse(fs.readFileSync(fixtureBatchPath, 'utf-8'))
  } catch (error) {
    throw new Error(`Fixture batch is invalid JSON: ${fixtureBatchPath}. ${error.message}`)
  }
}

export function getAbsoluteFixturePaths(batch) {
  // Always resolve paths relative to current workspace root to avoid stale cached paths
  const workspaceRoot = getWorkspaceRoot()
  return batch.files.map((item) => {
    // Use relative_path if available, fallback to absolute_path
    if (item.relative_path) {
      return path.join(workspaceRoot, item.relative_path)
    }
    return item.absolute_path
  })
}