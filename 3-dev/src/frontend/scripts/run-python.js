import { spawnSync } from 'node:child_process'
import process from 'node:process'

function resolvePythonCommand() {
  if (process.env.ASR_E2E_PYTHON) {
    return process.env.ASR_E2E_PYTHON
  }

  const candidates = process.platform === 'win32'
    ? ['python', 'py']
    : ['python3', 'python']

  for (const candidate of candidates) {
    const probe = spawnSync(candidate, ['--version'], { stdio: 'ignore' })
    if (probe.status === 0) {
      return candidate
    }
  }

  throw new Error(
    `Python interpreter not found. Tried: ${candidates.join(', ')}. `
    + 'Set ASR_E2E_PYTHON to override the command.',
  )
}

const args = process.argv.slice(2)

if (args.length === 0) {
  console.error('Usage: node scripts/run-python.js <script.py> [args...]')
  process.exit(1)
}

const pythonCommand = resolvePythonCommand()
const result = spawnSync(pythonCommand, args, {
  stdio: 'inherit',
  env: process.env,
})

if (result.error) {
  console.error(result.error.message)
  process.exit(1)
}

process.exit(result.status ?? 1)