import { expect, test } from '@playwright/test'


test.use({ viewport: { width: 1600, height: 1000 } })

test('usage reasons, export filters, request detail and keyboard dismissal', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  const base = {
    request_id: 'test-request-abcdef012345',
    created_at: '2026-09-14T12:15:33Z',
    endpoint: '/v1/chat/completions',
    model: 'spark-x1-4b',
    input_tokens: null,
    output_tokens: null,
    total_tokens: null,
    usage_reported: false,
    usage_quality: 'missing',
    api_key_id: 'test-key',
    key_name: '界面测试应用',
    prefix: 'test-only',
  }
  const rows = [
    {
      ...base,
      id: 'rejected',
      status: 'rejected',
      request_content: {
        available: true,
        payload: { model: 'spark-x1-4b', messages: [{ role: 'user', content: '请分析这次调用失败的原因' }], api_key: '[已隐藏敏感字段]' },
        raw_bytes: 126,
        truncated: false,
        redacted_fields: 1,
        binary_fields: 0,
      },
      diagnostics: {
        code: 'model_concurrency_exceeded',
        reason: '模型并发超限',
        suggestion: '应用侧排队，等待空闲后退避重试。',
        not_inferred: true,
        http_status: 429,
        source: 'historical_access_rejection',
        stage: 'admission',
        upstream_dispatched: false,
      },
    },
    {
      ...base,
      id: 'cancelled',
      status: 'cancelled',
      request_content: { available: false, payload: null, reason: '历史记录未保存请求内容' },
      diagnostics: {
        code: 'client_cancelled',
        reason: '调用连接已取消',
        suggestion: '核对应用取消、超时及网络日志；不能仅据此认定用户点击了停止。',
        not_inferred: false,
        http_status: 200,
        source: 'gateway',
        stage: 'stream',
        upstream_dispatched: true,
        elapsed_ms: 600109,
        first_output_ms: 251,
        finished_at: '2026-09-14T10:02:07Z',
      },
    },
  ]
  const queries: URL[] = []
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url())
    queries.push(url)
    if (url.pathname === '/api/usage/export') {
      return route.fulfill({ contentType: 'text/csv', body: '\ufeff时间,原因\n2026-09-14,模型并发超限\n' })
    }
    let data: unknown = {}
    if (url.pathname === '/api/session') data = { authenticated: true, csrf: 'test-only' }
    if (url.pathname === '/api/overview') data = { gateway: { status: 'ok', version: 'test', online_models: 1 }, host: {} }
    if (url.pathname === '/api/models' || url.pathname === '/api/api-keys') data = { data: [] }
    if (url.pathname === '/api/resource-policy') {
      data = { policy: { global_max_concurrent: 0, pool_limits: {}, model_limits: { 'spark-x1-4b': 2 } }, in_flight: { global: 0, key: {}, model: {}, pool: {} }, models: [], recent_rejections: [] }
    }
    if (url.pathname === '/api/usage') {
      const matching = url.searchParams.get('error_code') ? rows.slice(0, 1) : rows
      data = {
        summary: { request_count: matching.length, input_tokens: 0, output_tokens: 0, total_tokens: 0, unreported_requests: matching.length, admission_rejected_requests: 1 },
        by_key: [], by_model: [], daily: [], recent: matching,
        pagination: { offset: 0, total: matching.length, limit: 100, has_more: false },
        reason_options: [{ code: 'model_concurrency_exceeded', label: '模型并发超限' }],
      }
    }
    return route.fulfill({ json: data })
  })

  await page.goto('http://127.0.0.1:5175/usage')
  const rejected = page.getByRole('row').filter({ hasText: '模型并发超限' })
  await expect(rejected).toContainText('HTTP 429')
  await expect(rejected).toContainText('请求在网关被拒绝，未进入模型推理。')
  await expect(rejected).toContainText('建议：应用侧排队，等待空闲后退避重试。')
  await rejected.getByRole('button', { name: '查看内容与详情' }).click()
  const dialog = page.getByRole('dialog', { name: '请求详情' })
  await expect(dialog).toContainText('历史拒绝记录')
  await expect(dialog).toContainText('本次调用的请求内容')
  await expect(dialog).toContainText('请分析这次调用失败的原因')
  await expect(dialog).toContainText('1 个敏感字段已隐藏')
  await expect(dialog.getByRole('button', { name: '复制内容' })).toBeVisible()
  await expect(dialog).toContainText('未记录 / 未记录')
  await expect(dialog.getByText('test-request-abcdef012345', { exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(dialog).not.toBeVisible()
  await expect(rejected.getByRole('button', { name: '查看内容与详情' })).toBeFocused()

  await page.getByRole('row').filter({ hasText: '调用连接已取消' }).getByRole('button', { name: '查看内容与详情' }).click()
  await expect(dialog).toContainText('不能区分手动停止')
  await expect(dialog).toContainText('HTTP 200 仅表示响应头成功')
  await page.getByRole('button', { name: '关闭请求详情' }).click()
  await page.getByRole('combobox', { name: '具体原因', exact: true }).selectOption('model_concurrency_exceeded')
  await page.getByRole('button', { name: '查询', exact: true }).click()
  await expect(page.getByRole('row').filter({ hasText: '调用连接已取消' })).toHaveCount(0)
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出全部匹配记录' }).click()
  expect((await download).suggestedFilename()).toBe('modeldeck-usage.csv')
  expect(queries.find(query => query.pathname === '/api/usage/export')?.searchParams.get('error_code')).toBe('model_concurrency_exceeded')
  expect(queries.find(query => query.pathname === '/api/usage/export')?.searchParams.has('limit')).toBe(false)
  expect(errors).toEqual([])
})
