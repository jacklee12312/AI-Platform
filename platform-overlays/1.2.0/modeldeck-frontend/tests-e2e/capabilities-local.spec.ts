import { expect, test, type Page } from '@playwright/test'
import { readFile } from 'node:fs/promises'

test.skip(process.env.MODELDECK_CAPABILITY_FIXTURE !== '1', 'local complete capability fixture only')
async function login(page: Page) {
  await page.goto('/')
  await page.getByLabel('管理员密码').fill('audit-test-password')
  await page.getByRole('button', { name: '进入管理台' }).click()
  await page.getByRole('link', { name: '能力与示例', exact: true }).click()
  await expect(page.locator('.cap-card')).toHaveCount(24)
}
async function open(page: Page, model: string) {
  await page.getByRole('button', { name: `查看 ${model} 能力说明`, exact: true }).click()
  return page.getByRole('dialog', { name: `${model} 能力说明`, exact: true })
}

test('separate runtime, implementation and evidence; searchable limitations and export', async ({ page, baseURL }) => {
  expect(baseURL).toBe('http://127.0.0.1:18780')
  const errors: string[] = []; page.on('pageerror', e => errors.push(e.message))
  await login(page)
  await expect(page.locator('.cap-notice')).toContainText('注册 ≠ 推理可用 ≠ 质量达标 ≠ 正式交付')
  await page.screenshot({ path: '../../output/outputs/model-capabilities-20260907/desktop.png', fullPage: true })
  await page.getByLabel('按能力证据筛选').selectOption('partial')
  await expect(page.locator('.cap-card')).toHaveCount(2)
  const tracking = await open(page, 'bytetrack_s')
  await expect(tracking).toContainText('持续 track ID 未实现')
  await expect(tracking).toContainText('健康探测在线')
  await expect(tracking).toContainText('部分实现')
  await page.keyboard.press('Escape')
  await expect(tracking).toBeHidden()
  await page.getByLabel('按能力证据筛选').selectOption('all')
  await page.getByLabel('搜索模型能力').fill('CosyVoice')
  await expect(page.locator('.cap-card')).toHaveCount(1)
  const cosy = await open(page, 'cosyvoice3-0.5b')
  await expect(cosy).toContainText('未注册到网关')
  await expect(cosy).toContainText('不提供看似可执行的请求示例')
  await expect(cosy.getByLabel('可复制请求代码')).toHaveCount(0)
  await page.getByLabel('关闭能力说明').click()
  const downloading = page.waitForEvent('download')
  await page.getByRole('link', { name: '下载接入说明' }).click()
  const download = await downloading
  expect(download.suggestedFilename()).toBe('model-capabilities.md')
  const text = await readFile((await download.path())!, 'utf8')
  expect(text).toContain('不是正式交付验收证书')
  expect(text).toContain('SHA-256')
  expect(text).not.toContain('fixture-management')
  expect(errors).toEqual([])
})

test('deep links, executable language choices, actual clipboard and mobile layout', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await login(page)
  await page.goto('/capabilities?model=glm-ocr-0.9b')
  const dialog = page.getByRole('dialog')
  const code = dialog.getByLabel('可复制请求代码')
  await expect(code).toHaveValue(/Path\("image.png"\)/)
  await expect(dialog.getByLabel('示例调用方式')).toHaveValue('json')
  await dialog.getByLabel('示例语言').selectOption('powershell')
  await expect(code).toHaveValue(/curl.exe/)
  await expect(code).toHaveValue(/UTF8Encoding/)
  await dialog.getByRole('button', { name: '复制请求', exact: true }).click()
  expect((await page.evaluate(() => navigator.clipboard.readText())).replace(/\r\n/g, '\n')).toBe(await code.inputValue())
  await dialog.getByLabel('示例调用方式').selectOption('legacy')
  await expect(code).toHaveValue(/\/v1\/ocr\/mineru/)
  await expect(dialog).toContainText('不支持 stream=true')
  await page.reload()
  await expect(dialog).toBeVisible()
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  expect(await dialog.evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true)
  await page.screenshot({ path: '../../output/outputs/model-capabilities-20260907/mobile-detail.png' })
  await page.getByLabel('关闭能力说明').click()
  await page.getByLabel('搜索模型能力').fill('sensevoice')
  await open(page, 'sensevoice')
  await dialog.getByLabel('示例调用方式').selectOption('ws')
  await expect(dialog.getByLabel('示例语言').locator('option')).toHaveCount(1)
  await expect(code).toHaveValue(/additional_headers/)
  await expect(code).toHaveValue(/readframes\(1600\)/)
  await expect(dialog).toContainText('不保证逐字 partial 输出')
})

test('registry outage never offers executable requests', async ({ page }) => {
  await login(page)
  await page.route('**/api/model-capabilities', async route => {
    const response = await route.fetch(); const body = await response.json()
    body.registry_available = false
    for (const model of body.models) {
      model.registered = null; model.runtime_status = 'unknown'; model.examples = []; model.baseline_match = false
    }
    await route.fulfill({ json: body })
  })
  await page.locator('.cap-toolbar').getByRole('button', { name: '刷新状态' }).click()
  await expect(page.getByRole('alert')).toContainText('运行状态标为未知')
  const dialog = await open(page, 'copaw-flash-9b')
  await expect(dialog).toContainText('运行状态未知')
  await expect(dialog.getByLabel('可复制请求代码')).toHaveCount(0)
})

test('denied clipboard falls back to selected code with a truthful message', async ({ page }) => {
  await page.addInitScript(() => Object.defineProperty(navigator, 'clipboard', { value: { writeText: async () => { throw new Error('denied') } } }))
  await login(page)
  const dialog = await open(page, 'matcha-zh')
  await dialog.getByRole('button', { name: '复制请求', exact: true }).click()
  await expect(dialog.getByRole('status')).toContainText('代码已选中')
  await expect(dialog.getByRole('button', { name: '已复制', exact: true })).toHaveCount(0)
  const selected = await dialog.getByLabel('可复制请求代码').evaluate((el: HTMLTextAreaElement) => el.selectionEnd - el.selectionStart)
  expect(selected).toBe((await dialog.getByLabel('可复制请求代码').inputValue()).length)
})

test('failed refresh withdraws stale online claims and examples', async ({ page }) => {
  await login(page)
  await page.route('**/api/model-capabilities', route => route.fulfill({ status: 503, json: { detail: '能力状态暂不可用' } }))
  await page.locator('.cap-toolbar').getByRole('button', { name: '刷新状态' }).click()
  await expect(page.getByRole('alert')).toContainText('能力状态暂不可用')
  await expect(page.locator('.cap-card')).toHaveCount(0)
  await expect(page.getByLabel('可复制请求代码')).toHaveCount(0)
})
