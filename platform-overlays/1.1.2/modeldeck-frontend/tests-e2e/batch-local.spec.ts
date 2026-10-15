import { expect, test } from '@playwright/test'
import { readFile } from 'node:fs/promises'

test.skip(process.env.MODELDECK_AUDIT_FIXTURE !== '1', 'local fixture only')
const image = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAGQAAAAoCAIAAACHGsgUAAAAbklEQVR4nO3SsREAIAwDMWD/ncMK+V6qXf35zsxh5y13iNV4ViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYgViBWIFYg1tn7FeIDTQa3aB4AAAAASUVORK5CYII=', 'base64')
async function login(page: import('@playwright/test').Page) {
  await page.goto('/')
  await page.getByLabel('管理员密码').fill('audit-test-password')
  await page.getByRole('button', { name: '进入管理台' }).click()
  await page.getByRole('link', { name: '批量任务', exact: true }).click()
}

test('upload batch, retain background progress, preview and download partial results', async ({ page, baseURL }) => {
  expect(baseURL).toBe('http://127.0.0.1:18780')
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message))
  await login(page)
  const name = `LOCAL OCR ${Date.now()}`
  await page.getByLabel('任务名称').fill(name)
  await page.getByLabel('选择批量文件').setInputFiles([
    { name: 'good.png', mimeType: 'image/png', buffer: image },
    { name: 'broken.png', mimeType: 'image/png', buffer: Buffer.from('broken image') },
  ])
  await page.getByRole('button', { name: '上传并加入队列' }).click()
  const row = page.locator('tr[data-job-id]').filter({ hasText: name })
  await expect(row).toBeVisible()
  await page.reload()
  await expect(row).toContainText('部分成功', { timeout: 20000 })
  await expect(row).toContainText('成功 1')
  await row.getByRole('button', { name: '详情', exact: true }).click()
  await page.getByRole('button', { name: '查看文本' }).click()
  await expect(page.locator('.batch-preview pre')).toContainText('LOCAL BATCH OCR')
  const downloading = page.waitForEvent('download')
  await row.getByRole('link', { name: `下载 ${name} 结果` }).click()
  const download = await downloading
  const body = await readFile((await download.path())!)
  expect(body.subarray(0, 2).toString()).toBe('PK')
  await page.screenshot({ path: '../../output/outputs/batch-tasks-20260907/batch-desktop.png', fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await page.screenshot({ path: '../../output/outputs/batch-tasks-20260907/batch-mobile.png', fullPage: true })
  expect(errors).toEqual([])
})

test('queue cancellation and ASR WAV conversion', async ({ page, baseURL }) => {
  expect(baseURL).toBe('http://127.0.0.1:18780')
  await login(page)
  const name = `LOCAL cancel ${Date.now()}`
  await page.getByLabel('任务名称').fill(name)
  await page.getByLabel('选择批量文件').setInputFiles(Array.from({ length: 4 }, (_, i) => ({ name: `page-${i}.png`, mimeType: 'image/png', buffer: image })))
  await page.getByRole('button', { name: '上传并加入队列' }).click()
  const row = page.locator('tr[data-job-id]').filter({ hasText: name })
  await expect(row).toBeVisible(); await row.getByRole('button', { name: '取消', exact: true }).click()
  await expect(row).toContainText('已取消', { timeout: 15000 })
  const id = await row.getAttribute('data-job-id')
  const saved = await (await page.request.get(`/api/batches/${id}`)).json()
  expect(saved.items.filter((i: { status: string }) => i.status === 'succeeded').length).toBeLessThan(4)
  const audioName = `LOCAL ASR ${Date.now()}`
  const audio = Buffer.alloc(32044)
  audio.write('RIFF'); audio.writeUInt32LE(32036, 4); audio.write('WAVEfmt ', 8); audio.writeUInt32LE(16, 16); audio.writeUInt16LE(1, 20)
  audio.writeUInt16LE(1, 22); audio.writeUInt32LE(16000, 24); audio.writeUInt32LE(32000, 28); audio.writeUInt16LE(2, 32); audio.writeUInt16LE(16, 34)
  audio.write('data', 36); audio.writeUInt32LE(32000, 40)
  await page.getByRole('combobox', { name: '任务类型', exact: true }).selectOption('asr')
  await page.getByLabel('任务名称').fill(audioName)
  await page.getByLabel('选择批量文件').setInputFiles({ name: 'sample.wav', mimeType: 'audio/wav', buffer: audio })
  await page.getByRole('button', { name: '上传并加入队列' }).click()
  const audioRow = page.locator('tr[data-job-id]').filter({ hasText: audioName })
  await expect(audioRow).toContainText('已完成', { timeout: 15000 })
  await audioRow.getByRole('button', { name: '详情', exact: true }).click()
  await page.getByRole('button', { name: '查看文本' }).click()
  await expect(page.locator('.batch-preview pre')).toContainText('LOCAL BATCH ASR')
})
