import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests-e2e',
  timeout: 60_000,
  use: {
    baseURL: process.env.MODELDECK_BASE_URL || 'http://127.0.0.1:5173',
    channel: 'chrome',
    trace: 'retain-on-failure',
  },
  reporter: [['list']],
})
