import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'

const live = process.env.MODELDECK_VISIBILITY_FIXTURE
  ? JSON.parse(readFileSync(process.env.MODELDECK_VISIBILITY_FIXTURE, 'utf8')) : null
const overview = live?.overview || {
  gateway: { active_mode: 'cosyvoice', status: 'ok', version: 'test', models: {} },
  host: { system: {}, containers: [], modelctl: { healthy: true } },
}
const usage = live?.usage || {
  summary: { request_count: 1 }, by_key: [], by_model: [], daily: [],
  recent: [{
    id: 'test-content', request_id: 'cosyvoice-ui-acceptance-20260917',
    created_at: '2026-09-17T08:40:00Z', endpoint: '/v1/audio/speech',
    model: 'cosyvoice3-0.5b', key_name: '验收', prefix: 'test', status: 'succeeded',
    request_content: { available: true, payload: {
      input: '这是通过 ModelDeck API 网关完成的语音合成验收。',
      ref_text: '开饭时间早上9点至下午5点。', ref_audio: '[已省略 audio/wav base64 数据]',
    }, binary_fields: 1 },
  }],
}

test.use({ viewport: { width: 1024, height: 900 } })

async function mockApi(page: import('@playwright/test').Page, mode = 'cosyvoice') {
  const queries: URL[] = []
  await page.route('**/api/**', route => {
    const url = new URL(route.request().url())
    queries.push(url)
    const data: Record<string, unknown> = {
      '/api/session': { authenticated: true, csrf: 'fixture-only' },
      '/api/overview': { ...overview, gateway: { ...overview.gateway, active_mode: mode } },
      '/api/models': live?.models || { data: [] },
      '/api/api-keys': { data: [] },
      '/api/usage': usage,
      '/api/resource-policy': { policy: { global_max_concurrent: 0, pool_limits: {}, model_limits: {} }, in_flight: { global: 0, key: {}, model: {}, pool: {} }, models: [], recent_rejections: [] },
    }
    return route.fulfill({ json: data[url.pathname] || {} })
  })
  return queries
}

test('CosyVoice mode is named, switch is available and option is present', async ({ page }) => {
  await mockApi(page, 'cosyvoice ')
  await page.goto('/')
  await expect(page.getByText('CosyVoice3 独占模式', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: '切换模式', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: '切换模式', exact: true }).click()
  await page.getByRole('combobox').selectOption('cosyvoice')
  await expect(page.getByRole('combobox')).toHaveValue('cosyvoice')
  await expect(page.getByRole('option', { name: 'CosyVoice3 0.5B · 1 槽 / 2K' })).toHaveCount(1)
  await page.screenshot({ path: 'test-results/cosyvoice-mode-visible.png' })
})

test('unknown mode is explicit and does not strand the switch button', async ({ page }) => {
  await mockApi(page, 'future-mode')
  await page.goto('/')
  await expect(page.getByText('未识别的运行模式：future-mode')).toBeVisible()
  await expect(page.getByRole('button', { name: '切换模式', exact: true })).toBeEnabled()
  await expect(page.getByText('正在读取运行模式', { exact: true })).toHaveCount(0)
})

test('request deep link filters and exposes content without horizontal scrolling', async ({ page }) => {
  const queries = await mockApi(page)
  await page.goto('/usage?request_id=cosyvoice-ui-acceptance-20260917#request-details')
  const button = page.getByRole('button', { name: '查看内容与详情', exact: true }).first()
  await expect(button).toBeInViewport()
  expect(queries.some(url => url.pathname === '/api/usage' && url.searchParams.get('request_id') === 'cosyvoice-ui-acceptance-20260917')).toBe(true)
  await button.click()
  const dialog = page.getByRole('dialog', { name: '请求详情' })
  await expect(dialog).toContainText('这是通过 ModelDeck API 网关完成的语音合成验收。')
  await expect(dialog).toContainText('开饭时间早上9点至下午5点。')
  await expect(dialog).toContainText('1 个二进制字段已省略')
  await page.screenshot({ path: 'test-results/request-content-visible.png' })
})

test('a newer deployed build prompts a page update', async ({ page }) => {
  await mockApi(page)
  await page.route('**/build.json', route => route.fulfill({ json: { build: 'newer-build' } }))
  await page.goto('/')
  await expect(page.getByText('ModelDeck 已更新，当前页面仍是旧版本。请保存未提交的内容后更新页面。')).toBeVisible()
  await expect(page.getByRole('button', { name: '更新页面', exact: true })).toBeVisible()
})
