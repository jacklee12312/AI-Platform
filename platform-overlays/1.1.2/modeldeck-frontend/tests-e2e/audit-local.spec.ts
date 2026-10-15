import { expect, test } from '@playwright/test'

// Opt-in fixture-only suite. Never allow these simulated management actions to
// accidentally run against a production ModelDeck URL.
test.skip(process.env.MODELDECK_AUDIT_FIXTURE !== '1', 'requires the isolated S02 fixture')

test('audit records persist and show request/job correlation and final errors', async ({ page, baseURL }) => {
  expect(baseURL).toBe('http://127.0.0.1:18780')
  await page.goto('/')
  await page.getByLabel('管理员密码').fill('audit-test-password')
  await page.getByRole('button', { name: '进入管理台' }).click()
  await expect(page.getByRole('heading', { name: '运行总览' })).toBeVisible()
  const overview = await page.request.get('/api/overview')
  expect((await overview.json()).gateway.service).toBe('LOCAL S02 FIXTURE')

  await page.getByRole('link', { name: '模型管理' }).click()
  page.once('dialog', dialog => dialog.accept())
  await page.getByRole('button', { name: '重启 copaw-flash-9b' }).click()
  await expect(page.getByText('copaw-flash-9b 已完成重启并通过健康检查；输出质量需另行验证。', { exact: true })).toBeVisible()

  // A second simulated operation provides the failure case. API calls are to
  // the local fixture only, using the same real authentication and audit code.
  const session = await (await page.request.get('/api/session')).json()
  const failed = await page.request.post('/api/services/gateway/restart', {
    headers: { 'X-CSRF-Token': session.csrf, 'X-Request-ID': 'ui-failed-request' },
  })
  const job = await failed.json()
  expect(failed.status()).toBe(200)
  // Do not poll /api/jobs: completion must be collected by the server tracker.
  await expect.poll(async () => {
    const response = await page.request.get(`/api/audit?job_id=${job.id}`)
    return (await response.json()).data[0]?.status
  }).toBe('failed')

  await page.getByRole('link', { name: '日志审计' }).click()
  await page.getByRole('button', { name: '操作审计', exact: true }).click()
  await page.getByLabel('查询操作记录').fill(job.id)
  await page.getByRole('button', { name: '查询', exact: true }).click()
  await expect(page.locator('.audit-table tbody tr')).toHaveCount(1)
  await expect(page.locator('.audit-table')).toContainText('失败')
  await page.getByLabel('查看 ui-failed-request 的审计详情').click()
  await expect(page.locator('.audit-detail pre')).toContainText('模拟健康检查失败')
  await expect(page.locator('.audit-detail')).toContainText(job.id)
  await page.screenshot({ path: '../../.codex-tmp/s02-audit-desktop.png', fullPage: true })

  await page.getByLabel('查询操作记录').fill('nothing-matches-this')
  await page.getByRole('button', { name: '查询', exact: true }).click()
  await expect(page.getByText('暂无匹配的操作记录')).toBeVisible()
  await page.getByLabel('查询操作记录').fill('ui-failed-request')
  await page.getByRole('button', { name: '查询', exact: true }).click()
  await expect(page.locator('.audit-table')).toContainText('失败')
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByLabel('查询操作记录')).toBeVisible()
  const panel = await page.locator('.logs-panel').boundingBox()
  expect(panel!.x + panel!.width).toBeLessThanOrEqual(390)
  await page.screenshot({ path: '../../.codex-tmp/s02-audit-mobile.png', fullPage: true })
})
