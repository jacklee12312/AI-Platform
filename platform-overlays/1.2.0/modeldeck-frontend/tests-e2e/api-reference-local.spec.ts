import { expect, test } from '@playwright/test'
import { readFile } from 'node:fs/promises'

test.skip(process.env.MODELDECK_CAPABILITY_FIXTURE !== '1', 'local complete API reference fixture only')

async function login(page: import('@playwright/test').Page) {
  await page.goto('/')
  await page.getByLabel('管理员密码').fill('audit-test-password')
  await page.getByRole('button', { name: '进入管理台' }).click()
}

test('grouped navigation exposes the complete public contract and downloadable OpenAPI', async ({ page, baseURL }) => {
  expect(baseURL).toBe('http://127.0.0.1:18780')
  const errors: string[] = []
  page.on('pageerror', value => errors.push(value.message))
  await login(page)
  for (const label of ['使用模型', '接入管理', '模型运维', '系统交付']) {
    await expect(page.locator('.nav-group-label', { hasText: label })).toHaveCount(1)
  }
  await page.getByRole('link', { name: 'API 参考', exact: true }).click()
  await expect(page.getByRole('heading', { name: '完整 API 参考' })).toBeVisible()
  await expect(page.locator('.endpoint-list button')).toHaveCount(12)

  await page.getByRole('button', { name: '查看 POST /v1/chat/completions', exact: true }).click()
  const detail = page.locator('.endpoint-detail')
  await expect(detail).toContainText('messages[].content[].image_url.url')
  await expect(detail).toContainText('choices[].message.content')
  await expect(detail).toContainText('data: [DONE]')
  await expect(detail).toContainText('300 秒 · 当前保存配置')
  await expect(detail).toContainText('invalid_api_key')

  const downloading = page.waitForEvent('download')
  await page.getByRole('link', { name: '下载 OpenAPI', exact: true }).click()
  const download = await downloading
  expect(download.suggestedFilename()).toBe('modeldeck-openapi.json')
  const document = JSON.parse(await readFile((await download.path())!, 'utf8'))
  expect(document.openapi).toBe('3.1.0')
  expect(document.paths['/v1/chat/completions'].post.requestBody.content['application/json'].schema.$ref).toBe('#/components/schemas/ChatCompletionRequest')
  expect(document.paths['/v1/audio/tts'].post['x-alias-of']).toBe('/v1/audio/speech')
  expect(document.paths['/internal/api-keys']).toBeUndefined()
  expect(document['x-websocket-endpoints'][0].path).toBe('/v1/audio/transcriptions/stream')

  await page.getByRole('tab', { name: '流式与超时' }).click()
  await expect(page.locator('.protocol-grid')).toContainText('必须读到 data: [DONE]')
  await expect(page.getByText('ASR WebSocket 单连接音频上限', { exact: true })).toBeVisible()
  await page.getByRole('tab', { name: '错误码' }).click()
  await page.getByLabel('搜索 API 错误码').fill('npu_quarantined')
  await expect(page.getByText('检测到 NPU fatal，模型已隔离', { exact: true })).toBeVisible()
  expect(errors).toEqual([])
})

test('API reference remains usable on a phone viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await login(page)
  await page.getByRole('link', { name: 'API 参考', exact: true }).click()
  await expect(page.getByRole('heading', { name: '完整 API 参考' })).toBeVisible()
  await page.getByRole('button', { name: '查看 WS /v1/audio/transcriptions/stream', exact: true }).click()
  await expect(page.locator('.endpoint-detail')).toContainText('stop → done')
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
})
