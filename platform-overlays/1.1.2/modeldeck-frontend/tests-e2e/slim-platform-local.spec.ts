import { expect, test } from '@playwright/test'

const model = { id: 'copaw-flash-9b', capability: 'chat', enabled: true, base_url: 'http://copaw:17701', status: 'online', modes: ['copaw'], metadata: { device: 'LQ50' } }
const settings = { rate_limit_per_minute: 120, generation_defaults_enabled: false, asr_max_concurrency: 2 }
const schema = { version: 2, profiles: [], groups: [
  { id: 'gateway', label: '网关与接口', description: '请求限流', fields: [{ id: 'rate_limit_per_minute', label: '每分钟请求上限', type: 'integer', min: 10, max: 1000 }] },
  { id: 'generation', label: '模型默认值', description: '默认生成参数', fields: [] },
  { id: 'multimedia', label: 'ASR 与 CV', description: '资源限制', fields: [] },
] }

async function fixture(page: import('@playwright/test').Page) {
  const submitted: unknown[] = []
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (route.request().method() === 'POST') submitted.push(route.request().postDataJSON())
    const data: Record<string, unknown> = {
      '/api/session': { authenticated: true, csrf: 'local-fixture' },
      '/api/overview': { gateway: { active_mode: 'copaw', status: 'ok', version: '0.9.0', models: { [model.id]: { status: 'online', latency_ms: 10 } } }, host: { system: { cpu_cores: 8 }, containers: [], modelctl: { healthy: true } } },
      '/api/models': { object: 'list', data: [model] },
      '/api/config': { services: { gateway_version: '0.9.0' }, settings, schema, models: [model] },
      '/api/config/validate': { valid: true, changes: [{ id: 'rate_limit_per_minute', before: 120, after: 130 }], affected_services: ['gateway'], estimated_downtime_seconds: 10 },
      '/api/jobs': { data: [] },
    }
    await route.fulfill({ json: data[path] || {} })
  })
  return submitted
}

test('dashboard and configuration have no moderation controls; remaining configuration validates', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  const submitted = await fixture(page)
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '核心模型状态' })).toBeVisible()
  await expect(page.getByText('流式安全审核', { exact: true })).toHaveCount(0)
  await page.screenshot({ path: 'test-results/slim-dashboard.png', fullPage: true })
  await page.getByRole('link', { name: '配置管理' }).click()
  await expect(page.getByTestId('tab-gateway')).toBeVisible()
  await expect(page.getByTestId('tab-generation')).toBeVisible()
  await expect(page.getByTestId('tab-multimedia')).toBeVisible()
  await expect(page.getByTestId('tab-guard')).toHaveCount(0)
  await page.getByTestId('setting-rate_limit_per_minute').fill('130')
  await page.getByTestId('validate-config').click()
  await expect(page.getByTestId('impact-preview')).toBeVisible()
  expect(submitted).toHaveLength(1)
  expect(JSON.stringify(submitted[0])).not.toMatch(/guard|moderation|ballot/)
  await page.getByTestId('tab-models').click()
  await expect(page.getByText('启用模型', { exact: true })).toBeVisible()
  await expect(page.getByText('审核范围', { exact: true })).toHaveCount(0)
  await page.screenshot({ path: 'test-results/slim-config-models.png', fullPage: true })
  expect(errors).toEqual([])
})

test('configuration remains usable on a narrow screen', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await fixture(page)
  await page.goto('/')
  await page.getByRole('link', { name: '配置管理' }).click()
  await expect(page.getByTestId('setting-rate_limit_per_minute')).toBeVisible()
  await expect(page.getByTestId('tab-guard')).toHaveCount(0)
  await page.screenshot({ path: 'test-results/slim-config-mobile.png', fullPage: true })
})
