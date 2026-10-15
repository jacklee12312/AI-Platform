import { expect, test } from '@playwright/test'

test.skip(process.env.MODELDECK_LIVE_INSTALL_TEST !== '1', 'explicit live read-only acceptance only')
test.use({ trace: 'off', video: 'off' })

test('deployed installer displays catalog and preflight without submitting lifecycle actions', async ({ page, baseURL }, info) => {
  expect(baseURL).toBe('http://192.168.133.1:17780')
  const password = process.env.MODELDECK_ADMIN_PASSWORD
  expect(Boolean(password)).toBeTruthy()
  const errors: string[] = []
  const lifecycle: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => {
    if (request.method() === 'POST' && request.url().includes('/api/model-install/actions/')) lifecycle.push(request.url())
  })
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/model-install')
  await page.getByLabel('管理员密码').fill(password!)
  await page.getByRole('button', { name: '进入管理台' }).click()
  await page.getByRole('link', { name: '模型安装', exact: true }).click()
  await expect(page.getByRole('heading', { name: '按需安装已适配模型' })).toBeVisible()
  await expect(page.locator('tbody tr')).toHaveCount(23, { timeout: 60_000 })
  await expect(page.getByRole('heading', { name: '离线交付包' })).toBeVisible()
  await page.getByLabel('选择 matcha-zh', { exact: true }).check()
  await page.getByLabel('安装后立即启用并自测（可能重启共享服务）').check()
  await page.getByRole('button', { name: '安装预检', exact: true }).click()
  await expect(page.locator('.install-actions')).toContainText('预检通过', { timeout: 60_000 })
  await expect(page.getByRole('button', { name: '安装并启用', exact: true })).toBeEnabled()
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: info.outputPath('model-install-live-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  const table = page.locator('.management-table')
  await expect(table).toHaveCSS('min-width', '900px')
  expect(await table.evaluate(element => element.getBoundingClientRect().width)).toBeGreaterThan(850)
  await page.screenshot({ path: info.outputPath('model-install-live-mobile.png'), fullPage: true })
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.getByRole('link', { name: '模型管理', exact: true }).click()
  await expect(page.getByText('共 23 个模型，22 个在线', { exact: true })).toBeVisible({ timeout: 60_000 })
  await page.screenshot({ path: info.outputPath('model-management-live.png'), fullPage: true })
  expect(lifecycle).toEqual([])
  expect(errors).toEqual([])
})
