import { expect, test } from '@playwright/test'

test.skip(process.env.MODELDECK_MODE_SWITCH_FIXTURE !== '1', 'local verified mode-switch fixture only')

test('mode switch shows current model and only closes after verification', async ({ page, baseURL }) => {
  expect(baseURL).toBe('http://127.0.0.1:18786')
  await page.goto('/')
  await page.getByLabel('管理员密码').fill('audit-test-password')
  await page.getByRole('button', { name: '进入管理台' }).click()
  await page.getByRole('button', { name: '切换模式' }).click()
  await page.getByRole('button', { name: '确认切换到 Qwen3.8 独占模式' }).click()
  const dialog = page.getByRole('dialog', { name: '切换运行模式' })
  await expect(dialog).toContainText('当前模型：')
  await expect(dialog).toContainText('qwen3.8-27b')
  await expect(dialog).toContainText('已用时')
  await expect(dialog).toContainText('全部通过才会切换成功')
  await expect(dialog).toHaveCount(0, { timeout: 8_000 })
  await expect(page.getByText('模式切换完成，模型状态已重新校验。', { exact: true })).toBeVisible()
})
