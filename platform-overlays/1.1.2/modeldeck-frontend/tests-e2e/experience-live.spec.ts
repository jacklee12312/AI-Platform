import { expect, test } from '@playwright/test'
import { readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'

test.skip(process.env.MODELDECK_LIVE_EXPERIENCE_TEST !== '1', 'explicit bounded live media acceptance only')
test.use({ trace: 'off', video: 'off' })

test('deployed media experience uses real responses and respects unavailable models', async ({ page, baseURL }, info) => {
  test.setTimeout(600000)
  page.setDefaultTimeout(20000)
  expect(baseURL).toBe('http://192.168.133.1:17780')
  expect(Boolean(process.env.MODELDECK_ADMIN_PASSWORD)).toBeTruthy()
  const errors: string[] = [], lifecycle: string[] = [], calls: string[] = [], reports: any[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => {
    if (request.method() !== 'POST') return
    if (/restart|mode-switch|model-install\/actions|recovery/.test(request.url())) lifecycle.push(request.url())
    if (request.url().includes('/api/test/')) calls.push(request.url())
  })
  const record = async (value: any) => {
    reports.push(value)
    await writeFile(info.outputPath('experience-live-summary.json'), JSON.stringify({ reports, errors, lifecycle, calls }, null, 2))
    console.log(JSON.stringify(value))
  }
  await page.setViewportSize({ width: 1600, height: 1050 })
  await page.goto('/test')
  await page.getByLabel('管理员密码').fill(process.env.MODELDECK_ADMIN_PASSWORD!)
  await page.getByRole('button', { name: '进入管理台' }).click()
  await page.getByRole('link', { name: '模型体验', exact: true }).click()
  await expect(page.getByRole('heading', { name: '模型体验', exact: true })).toBeVisible()
  const inventory = (await (await page.request.get('/api/models')).json()).data as { id: string; status: string }[]
  await record({ kind: 'inventory', models: inventory.map(({ id, status }) => ({ id, status })) })

  // A repository-owned smoke image; assert rendering of the real returned
  // detections, never manufacture boxes if this model finds no COCO targets.
  await page.locator('[data-experience="cv"]').click()
  await expect(page.getByLabel('模型', { exact: true })).toHaveValue('yolov11_n')
  await page.getByLabel('检测图片').setInputFiles(fileURLToPath(new URL('../../../cv_deploy/test.jpg', import.meta.url)))
  const cvResponse = page.waitForResponse(response => response.url().includes('/api/test/cv/') && response.request().method() === 'POST', { timeout: 190000 })
  await page.getByRole('button', { name: '发送请求', exact: true }).click()
  const cvHttp = await cvResponse
  expect(cvHttp.status()).toBe(200)
  const cv = await cvHttp.json()
  await expect(page.locator('.exp-success')).toHaveText('完成')
  expect(cv.original_size).toEqual([500, 346])
  const rows = cv.result.detections
  await expect(page.locator('[data-detection-id]')).toHaveCount(rows.length)
  if (!rows.length) await expect(page.getByText('本次阈值下未检出目标，不代表图片中一定没有目标。')).toBeVisible()
  await writeFile(info.outputPath('experience-live-cv.json'), JSON.stringify(cv, null, 2))
  await page.screenshot({ path: info.outputPath('experience-live-cv.png'), fullPage: true })
  await record({ kind: 'cv', model: cv.model, http: cvHttp.status(), detections: rows.length })

  await page.locator('[data-experience="tts"]').click()
  await page.getByLabel('合成文字').fill('你好，欢迎使用模型体验中心。')
  const speechResponse = page.waitForResponse(response => response.url().endsWith('/api/test/tts'), { timeout: 190000 })
  await page.getByRole('button', { name: '生成语音', exact: true }).click()
  const speechHttp = await speechResponse
  expect(speechHttp.status()).toBe(200)
  await expect(page.locator('.exp-success')).toHaveText('完成')
  const player = page.locator('audio[aria-label="合成音频播放器"]')
  await expect.poll(() => player.evaluate(el => (el as HTMLAudioElement).readyState)).toBeGreaterThanOrEqual(1)
  expect(await player.evaluate(el => (el as HTMLAudioElement).paused)).toBe(true)
  await player.evaluate(el => (el as HTMLAudioElement).play())
  await expect.poll(() => player.evaluate(el => (el as HTMLAudioElement).currentTime)).toBeGreaterThan(.1)
  await player.evaluate(el => (el as HTMLAudioElement).pause())
  const duration = await player.evaluate(el => (el as HTMLAudioElement).duration)
  const download = page.waitForEvent('download')
  await page.getByRole('link', { name: '下载 WAV', exact: true }).click()
  const wavPath = info.outputPath('experience-live-speech.wav')
  await (await download).saveAs(wavPath)
  const wav = await readFile(wavPath)
  expect(wav.toString('ascii', 0, 4)).toBe('RIFF')
  await page.screenshot({ path: info.outputPath('experience-live-tts.png'), fullPage: true })
  await record({ kind: 'tts', model: 'matcha-zh', http: speechHttp.status(), bytes: wav.length, duration, played: true })

  await page.locator('[data-experience="asr"]').click()
  await page.getByLabel('识别音频').setInputFiles({ name: '模型体验合成样本.wav', mimeType: 'audio/wav', buffer: wav })
  const asrResponse = page.waitForResponse(response => response.url().includes('/api/test/asr?'), { timeout: 190000 })
  await page.getByRole('button', { name: '发送请求', exact: true }).click()
  const asrHttp = await asrResponse
  expect(asrHttp.status()).toBe(200)
  const asr = await asrHttp.json()
  expect(asr.text.trim().length).toBeGreaterThan(0)
  await expect(page.locator('.text-result pre')).toHaveText(asr.text)
  await page.locator('audio').evaluate(el => (el as HTMLAudioElement).play())
  await expect.poll(() => page.locator('audio').evaluate(el => (el as HTMLAudioElement).currentTime)).toBeGreaterThan(.1)
  await page.locator('audio').evaluate(el => (el as HTMLAudioElement).pause())
  await page.screenshot({ path: info.outputPath('experience-live-asr.png'), fullPage: true })
  await record({ kind: 'asr', model: 'sensevoice', http: asrHttp.status(), text: asr.text, segments: asr.segments?.length || 0, played: true })
  await page.setViewportSize({ width: 390, height: 844 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true)
  await page.screenshot({ path: info.outputPath('experience-live-mobile.png'), fullPage: true })
  await page.setViewportSize({ width: 1600, height: 1050 })

  const ocrImage = await page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = 320; canvas.height = 200
    const ctx = canvas.getContext('2d')!; ctx.fillStyle = 'white'; ctx.fillRect(0, 0, 320, 200)
    ctx.fillStyle = 'black'; ctx.font = 'bold 100px sans-serif'; ctx.fillText('7', 125, 140)
    return canvas.toDataURL().split(',')[1]!
  })
  await page.locator('[data-experience="ocr"]').click()
  await page.getByLabel('识别图片').setInputFiles({ name: 'digit-7.png', mimeType: 'image/png', buffer: Buffer.from(ocrImage, 'base64') })
  await expect(page.getByAltText('本次 OCR 原图')).toBeVisible()
  const ocrState = inventory.find(model => model.id === 'glm-ocr-0.9b')?.status
  if (ocrState !== 'online') {
    await expect(page.getByRole('button', { name: '发送请求', exact: true })).toBeDisabled()
    await record({ kind: 'ocr', status: 'skipped', reason: ocrState, unavailable_model_disabled: true })
  } else {
    await page.getByRole('button', { name: '发送请求', exact: true }).click()
    await expect(page.locator('.exp-success')).toHaveText('完成', { timeout: 190000 })
    const text = await page.locator('.text-result pre').innerText()
    expect(text.trim().length).toBeGreaterThan(0)
    await record({ kind: 'ocr', status: 'completed', text })
  }
  await page.screenshot({ path: info.outputPath('experience-live-ocr.png'), fullPage: true })
  const after = (await (await page.request.get('/api/models')).json()).data as { id: string; status: string }[]
  expect(after.filter(model => model.status === 'error').length).toBeLessThanOrEqual(inventory.filter(model => model.status === 'error').length)
  await record({ kind: 'checks', errors, lifecycle, inference_calls: calls.length })
  expect(lifecycle).toEqual([]); expect(errors).toEqual([])
})
