import { expect, test } from '@playwright/test'
import { writeFile } from 'node:fs/promises'

test.skip(process.env.MODELDECK_LIVE_EVALUATION_TEST !== '1', 'explicit bounded live inference acceptance only')
test.use({ trace: 'off', video: 'off' })

test('deployed center performs bounded serial inference and preserves evidence', async ({ page, baseURL }, info) => {
  test.setTimeout(900000)
  page.setDefaultTimeout(30000)
  expect(baseURL).toBe('http://192.168.133.1:17780')
  expect(Boolean(process.env.MODELDECK_ADMIN_PASSWORD)).toBeTruthy()
  const errors: string[] = [], lifecycle: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => {
    if (request.method() !== 'GET' && /restart|mode-switch|model-install\/actions|recovery/.test(request.url())) lifecycle.push(request.url())
  })
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/evaluations')
  await page.getByLabel('管理员密码').fill(process.env.MODELDECK_ADMIN_PASSWORD!)
  await page.getByRole('button', { name: '进入管理台' }).click()
  await page.getByRole('link', { name: '自检与评测', exact: true }).click()
  await expect(page.locator('[data-model-id]')).toHaveCount(23, { timeout: 60000 })
  await expect(page.getByLabel('选择模型 qwen3.8-27b', { exact: true })).toBeDisabled()
  await expect(page.locator('[data-model-id] input:enabled')).toHaveCount(22)
  const suites = (process.env.MODELDECK_EVALUATION_SUITES || 'smoke,quality,repeat').split(',')
  expect(suites.every(suite => ['smoke', 'quality', 'repeat'].includes(suite))).toBeTruthy()
  expect(new Set(suites).size).toBe(suites.length)
  await page.getByRole('button', { name: '全选当前可用', exact: true }).click()
  const reports = []
  for (const suite of suites) {
    if (suite !== 'smoke') {
      const clear = page.getByRole('button', { name: '清空选择', exact: true })
      if (await clear.isEnabled()) await clear.click()
      await page.locator(`input[type="radio"][value="${suite}"]`).check()
      for (const model of suite === 'quality'
        ? ['copaw-flash-9b', 'qwen3-vl-8b', 'bge-m3', 'bge-reranker-v2-m3', 'qwen3guard-stream-0.6b', 'glm-ocr-0.9b']
        : ['matcha-zh']) await page.getByLabel(`选择模型 ${model}`, { exact: true }).check()
      if (suite === 'repeat') await page.getByLabel('每个用例重复次数').selectOption('3')
    }
    await page.getByRole('button', { name: '确认并开始评测', exact: true }).click()
    await page.getByRole('dialog').getByRole('checkbox').check()
    const posted = page.waitForResponse(response => response.url().endsWith('/api/evaluations') && response.request().method() === 'POST')
    await page.getByRole('button', { name: '开始真实推理', exact: true }).click()
    const response = await posted
    expect(response.ok()).toBeTruthy()
    const accepted = await response.json()
    await page.reload()
    await expect.poll(async () => {
      const res = await page.request.get(`/api/evaluations/${accepted.id}`)
      return (await res.json()).status
    }, { timeout: 240000, intervals: [3000] }).not.toMatch(/^(queued|running|cancelling)$/)
    const report = await (await page.request.get(`/api/evaluations/${accepted.id}`)).json()
    reports.push({ id: report.id, suite, status: report.status, verdict: report.verdict, summary: report.summary })
    await writeFile(info.outputPath(`evaluation-live-${suite}.json`), JSON.stringify(report, null, 2))
    console.log(JSON.stringify(reports[reports.length - 1]))
    await page.getByRole('button', { name: '查询状态', exact: true }).click()
    await expect(page.locator('.evaluation-result-head')).not.toContainText('评测中', { timeout: 15000 })
    const download = page.waitForEvent('download')
    await page.getByRole('link', { name: '导出 Markdown', exact: true }).click()
    await (await download).saveAs(info.outputPath(`evaluation-live-${suite}.md`))
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.screenshot({ path: info.outputPath(`evaluation-live-${suite}.png`), fullPage: true })
    // Inference quality may legitimately fail; execution errors must be investigated.
    expect(report.status).toBe('completed')
  }
  await writeFile(info.outputPath('evaluation-live-summary.json'), JSON.stringify(reports, null, 2))
  await page.getByRole('link', { name: '模型管理', exact: true }).click()
  await expect(page.getByText('共 23 个模型，22 个在线', { exact: true })).toBeVisible({ timeout: 60000 })
  expect(lifecycle).toEqual([])
  expect(errors).toEqual([])
})
