import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { defineConfig } from '@playwright/test'

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const frontendRoot = __dirname
const backendRoot = path.resolve(__dirname, '../backend')
const nodeExecutable = process.execPath
const backendStartScript = path.join(frontendRoot, 'scripts', 'start-backend.js')
const profile = process.env.ASR_E2E_PROFILE || 'smoke'
const timeoutByProfile = {
  smoke: 10 * 60 * 1000,
  'remote-standard': 20 * 60 * 1000,
  standard: 30 * 60 * 1000,
  full: 90 * 60 * 1000,
}
const testTimeout = timeoutByProfile[profile] || timeoutByProfile.smoke

function quoteShellArg(value) {
  return /[\s"]/u.test(value) ? `"${value.replace(/"/g, '\\"')}"` : value
}

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list']],
  timeout: testTimeout,
  use: {
    baseURL: process.env.ASR_E2E_BASE_URL || 'http://127.0.0.1:15798',
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: 'off',
  },
  webServer: [
    {
      command: `${quoteShellArg(nodeExecutable)} ${quoteShellArg(backendStartScript)}`,
      cwd: backendRoot,
      url: 'http://127.0.0.1:15797/health',
      reuseExistingServer: true,
      timeout: 180 * 1000,
    },
    {
      command: 'npx vite --host 127.0.0.1 --port 15798',
      cwd: frontendRoot,
      url: 'http://127.0.0.1:15798',
      reuseExistingServer: true,
      timeout: 120 * 1000,
    },
  ],
})