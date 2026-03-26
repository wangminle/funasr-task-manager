import { spawn, spawnSync } from 'node:child_process'
import process from 'node:process'

function resolvePythonCommand() {
  if (process.env.ASR_E2E_BACKEND_PYTHON) {
    return process.env.ASR_E2E_BACKEND_PYTHON
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
    + 'Set ASR_E2E_BACKEND_PYTHON to override the command.',
  )
}

function runStep(command, args) {
  const result = spawnSync(command, args, {
    stdio: 'inherit',
    env: process.env,
  })

  if (result.error) {
    throw result.error
  }

  if (result.status !== 0) {
    process.exit(result.status ?? 1)
  }
}

const pythonCommand = resolvePythonCommand()

runStep(pythonCommand, ['-m', 'alembic', 'upgrade', 'head'])

const serverProcess = spawn(
  pythonCommand,
  ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', '8000'],
  {
    stdio: 'inherit',
    env: process.env,
  },
)

const forwardSignal = (signal) => {
  if (!serverProcess.killed) {
    serverProcess.kill(signal)
  }
}

process.on('SIGINT', () => forwardSignal('SIGINT'))
process.on('SIGTERM', () => forwardSignal('SIGTERM'))

serverProcess.on('exit', (code) => {
  process.exit(code ?? 0)
})

serverProcess.on('error', (error) => {
  console.error(error.message)
  process.exit(1)
})