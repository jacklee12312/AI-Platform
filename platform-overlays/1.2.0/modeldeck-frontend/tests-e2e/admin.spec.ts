import { expect, test } from '@playwright/test'

const password = process.env.MODELDECK_PASSWORD || 'ModelDeck-Local-2026!'

async function login(page: import('@playwright/test').Page) {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '登录管理台' })).toBeVisible()
  await page.getByLabel('管理员密码').fill(password)
  await page.getByRole('button', { name: '进入管理台' }).click()
  await expect(page.getByRole('heading', { name: '运行总览' })).toBeVisible()
}

test('desktop navigation and real streaming request', async ({ page }) => {
  const consoleErrors: string[] = []
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()) })
  await login(page)
  await expect(page.getByText('当前运行模式')).toBeVisible()
  await expect(page.getByText('qwen3guard-stream-0.6b', { exact: true })).toBeVisible()
  await page.screenshot({ path: '../../.codex-tmp/modeldeck-desktop.png', fullPage: true })

  await page.getByRole('link', { name: '模型管理' }).click()
  await expect(page.getByRole('heading', { name: '模型管理' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '模型注册表' })).toBeVisible()

  await page.getByRole('link', { name: '模型体验' }).click()
  await expect(page.getByRole('heading', { name: '模型体验', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '发送请求' }).click()
  await expect(page.getByText('完成', { exact: true })).toBeVisible({ timeout: 40_000 })
  await expect(page.locator('.chat-result')).toContainText('1')
  await page.locator('.experience-debug summary').click()
  await expect(page.getByText('[DONE]', { exact: true })).toBeVisible()

  await page.getByRole('link', { name: '配置管理' }).click()
  await expect(page.getByRole('heading', { name: '配置管理' })).toBeVisible()
  await expect(page.getByText('面向交付人员的安全配置')).toBeVisible()
  await expect(page.getByTestId('tab-gateway')).toBeVisible()
  await expect(page.getByTestId('tab-generation')).toBeVisible()
  await expect(page.getByTestId('tab-guard')).toBeVisible()
  await expect(page.getByTestId('tab-multimedia')).toBeVisible()
  await expect(page.getByTestId('setting-rate_limit_per_minute')).toBeVisible()
  await page.getByTestId('tab-guard').click()
  await expect(page.getByTestId('setting-guard_mode')).toBeVisible()
  await page.screenshot({ path: '../../.codex-tmp/modeldeck-config-desktop.png', fullPage: true })
  await page.getByRole('link', { name: '日志审计' }).click()
  await expect(page.getByRole('heading', { name: '日志审计' })).toBeVisible()
  expect(consoleErrors).toEqual([])
})

test('mobile layout remains operable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await login(page)
  await expect(page.getByRole('link', { name: '模型体验' })).toBeVisible()
  await expect(page.getByText('CoPaw 独占模式')).toBeVisible({ timeout: 10_000 })
  await page.getByRole('link', { name: '配置管理' }).click()
  await expect(page.getByTestId('setting-rate_limit_per_minute')).toBeVisible()
  await page.screenshot({ path: '../../.codex-tmp/modeldeck-config-mobile.png', fullPage: true })
})

test('only two modes are offered and inactive models cannot be restarted', async ({ page }) => {
  await login(page)
  await page.getByRole('button', { name: '切换模式' }).click()
  const modeSelect = page.getByLabel('目标模式')
  await expect(modeSelect.locator('option')).toHaveCount(2)
  await expect(modeSelect.locator('option[value="spark-x1"]')).toContainText('Spark X1')
  await expect(modeSelect.locator('option[value="glm-ocr"]')).toContainText('GLM-OCR')
  await expect(modeSelect.locator('option[value="ocr"]')).toHaveCount(0)
  await page.getByRole('button', { name: '取消' }).click()

  await page.getByRole('link', { name: '模型管理' }).click()
  const qwen38Row = page.getByRole('row').filter({ hasText: 'qwen3.8-27b' })
  const ocrRow = page.getByRole('row').filter({ hasText: 'glm-ocr-0.9b' })
  await expect(qwen38Row.getByRole('button', { name: '重启 qwen3.8-27b' })).toBeDisabled()
  await expect(ocrRow.getByRole('button', { name: '重启 glm-ocr-0.9b' })).toBeEnabled()
})

test('API Key lifecycle and per-Key usage page is operable', async ({ page }) => {
  const consoleErrors: string[] = []
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()) })
  await login(page)
  await page.getByRole('link', { name: 'Key 与用量' }).click()
  await expect(page.getByRole('heading', { name: 'Key 管理' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '用量查询与汇总' })).toBeVisible()

  const keyTable = page.locator('.key-panel')
  const keyARow = keyTable.getByRole('row').filter({ hasText: '__acceptance__A' })
  const keyBRow = keyTable.getByRole('row').filter({ hasText: '__acceptance__B' })
  await expect(keyARow).toContainText('26')
  await expect(keyBRow).toContainText('24')
  await expect(page.getByText('未报告 usage', { exact: true })).toHaveCount(0)

  await page.getByRole('button', { name: '新建 API Key' }).click()
  await expect(page.getByRole('heading', { name: '新建 API Key' })).toBeVisible()
  await page.getByLabel('Key 名称').fill('浏览器验收（不提交）')
  await page.getByRole('button', { name: '取消' }).click()
  await expect(page.getByRole('heading', { name: '新建 API Key' })).toHaveCount(0)
  await page.screenshot({ path: '../../.codex-tmp/modeldeck-usage-desktop.png', fullPage: true })
  expect(consoleErrors).toEqual([])
})
