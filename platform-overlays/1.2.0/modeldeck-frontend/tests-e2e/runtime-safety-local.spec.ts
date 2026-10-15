import { expect, test } from '@playwright/test'

test.skip(process.env.MODELDECK_RUNTIME_FIXTURE !== '1', 'local API fixtures only')

test('fault isolation, first evidence and mobile layout remain visible despite Healthy container', async ({ page, baseURL }) => {
  expect(baseURL).toBe('http://127.0.0.1:18783')
  const errors:string[]=[]; page.on('pageerror',error=>errors.push(error.message))
  const safety={status:'error',reason:'npu_quarantined',message:'NPU 故障隔离，已停止接收新请求',
    incident:{id:'fixture-first-fault',at:'2026-09-07T00:00:00Z',kind:'npu_fatal',source:'houmo-copaw',detail:'SyncYieldErr Core 0 Tile 2'}}
  await page.route('**/api/**',async route=>{
    const path=new URL(route.request().url()).pathname
    if(path==='/api/session')return route.fulfill({json:{authenticated:true,csrf:'fixture-csrf'}})
    if(path==='/api/overview')return route.fulfill({json:{gateway:{status:'degraded',version:'local-test',active_mode:'copaw',online_models:0,total_models:1,models:{'copaw-flash-9b':safety}},host:{system:{},containers:[{service:'copaw',state:'running',status:'Up (healthy)'}],modelctl:{configured_mode:'copaw'}},refreshed_at:'2026-09-07T00:00:01Z'}})
    if(path==='/api/models')return route.fulfill({json:{object:'list',data:[{id:'copaw-flash-9b',status:'error',safety,capability:'chat',metadata:{device:'LQ50'},modes:['copaw']}]}})
    return route.fulfill({status:404,json:{detail:'fixture has no mutation endpoints'}})
  })
  await page.goto('/models')
  const notice=page.getByRole('alert',{name:'NPU 请求隔离状态'})
  await expect(notice).toContainText('NPU 故障已隔离')
  await page.getByText('查看首个故障证据').click()
  await expect(notice).toContainText('SyncYieldErr Core 0 Tile 2')
  await expect(page.getByRole('button',{name:'重启 copaw-flash-9b'})).toBeDisabled()
  await page.getByRole('button',{name:'重新检查',exact:true}).click()
  await expect(notice).toContainText('fixture-first-fault')
  await page.screenshot({path:'../../output/outputs/runtime-resilience-20260907/isolation-desktop.png',fullPage:true})
  await page.setViewportSize({width:390,height:844})
  expect(await page.evaluate(()=>document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
  await page.screenshot({path:'../../output/outputs/runtime-resilience-20260907/isolation-mobile.png',fullPage:true})
  expect(errors).toEqual([])
})

test('cancelled batch with unknown inference keeps Request ID and partial download visible',async({page,baseURL})=>{
  expect(baseURL).toBe('http://127.0.0.1:18783')
  const job={id:'fixture-batch',name:'取消后连接中断',kind:'ocr',status:'interrupted',created_at:1788750000,total:2,completed:2,progress:100,queue_position:0,
    counts:{interrupted:1,cancelled:1},items:[{id:'unknown',filename:'one.pdf',status:'interrupted',size:123,pages:2,completed_pages:1,stage:'处理被中断',error:'执行结果未知，未自动重试',warning:'',has_result:true,request_id:'batch-fixture-page2'}]}
  await page.route('**/api/**',async route=>{
    const path=new URL(route.request().url()).pathname
    if(path==='/api/session')return route.fulfill({json:{authenticated:true,csrf:'fixture-csrf'}})
    if(path==='/api/overview')return route.fulfill({json:{gateway:{status:'ok',models:{}},host:{system:{},containers:[],modelctl:{}}}})
    if(path==='/api/models')return route.fulfill({json:{data:[]}})
    if(path==='/api/batches')return route.fulfill({json:{data:[job],storage_bytes:123,storage_limit_bytes:2048}})
    if(path==='/api/batches/fixture-batch')return route.fulfill({json:job})
    return route.fulfill({status:404,json:{detail:'local fixture'}})
  })
  await page.goto('/batches');await page.getByRole('button',{name:'取消后连接中断',exact:true}).click()
  await expect(page.getByRole('alert')).toContainText('最终结果未知，未自动重试')
  await expect(page.locator('.batch-request')).toContainText('batch-fixture-page2')
  await expect(page.getByRole('link',{name:'下载 取消后连接中断 结果',exact:true})).toBeVisible()
  await page.screenshot({path:'../../output/outputs/runtime-resilience-20260907/batch-interrupted.png',fullPage:true})
})
