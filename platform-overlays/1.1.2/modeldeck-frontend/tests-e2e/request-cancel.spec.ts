import { test, expect } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'

test.use({ viewport: { width: 1600, height: 1000 } })
test('cancellation confirmation, pending occupancy, and completed record', async ({page}) => {
  const errors:string[]=[]; page.on('pageerror', e=>errors.push(e.message))
  let count=4, fail=false, cancelled=false, cancelPosts=0
  const row={cancel_supported:true,id:'a'.repeat(32),request_id:'ui-monitor-test',created_at:new Date().toISOString(),endpoint:'/v1/chat/completions',model:'qwen3.8-27b',api_key_id:'test',key_name:'知识库测试任务',prefix:'',status:'in_flight',phase:'active',input_tokens:null,output_tokens:null,total_tokens:null,usage_reported:false,request_summary:'请归纳测试文档',request_metadata:{client_ip:'172.18.0.7',stream:false,message_count:2,tool_count:0,user_agent:'test-worker'},diagnostics:{stage:'upstream',elapsed_ms:95200,started_at:new Date().toISOString(),in_flight:4,limit:4,scope:'model'}}
  const policy={global_max_concurrent:0,model_limits:{'qwen3.8-27b':4},pool_limits:{}}
  await page.route('**/api/**', async route=>{
    const url=new URL(route.request().url());let data:unknown={}
    if(url.pathname==='/api/session') data={authenticated:true,csrf:'test'}
    if(url.pathname==='/api/overview') data={gateway:{status:'ok',version:'test',online_models:1},host:{}}
    if(['/api/models','/api/api-keys'].includes(url.pathname)) data={data:[]}
    if(url.pathname==='/api/resource-policy') data={policy,in_flight:{global:count,model:{'qwen3.8-27b':count},key:{},pool:{}},models:[],recent_rejections:[]}
    if(url.pathname==='/api/usage') data={summary:{request_count:0,input_tokens:0,output_tokens:0,total_tokens:0,unreported_requests:0},by_key:[],by_model:[],daily:[],recent:[],pagination:{offset:0,total:0,limit:100,has_more:false},reason_options:[]}
    if(url.pathname.endsWith('/cancel') && route.request().method()==='POST') { cancelled=true;cancelPosts++;return route.fulfill({status:202,json:{status:'cancelling',message:'已发送中断信号，等待上游连接关闭后释放名额'}}) }
    if(url.pathname==='/api/requests/active') {
      if(fail) return route.fulfill({status:503,json:{detail:'测试连接失败'}})
      data={observed_at:new Date().toISOString(),total:count,truncated:false,requests:Array.from({length:count},(_,i)=>({...row,cancel_requested:cancelled&&i===0,id:String.fromCharCode(97+i).repeat(32),request_id:'ui-monitor-test-'+i})),in_flight:{global:count,model:{'qwen3.8-27b':count},key:{test:count},pool:{}},policy,models:[]}
    } else if(url.pathname.startsWith('/api/requests/')) data={...row,cancel_requested:cancelled,id:url.pathname.split('/').pop(),status:count?'in_flight':'cancelled',request_content:{available:true,payload:{messages:[{role:'user',content:'请归纳测试文档'}],max_tokens:128,password:'[已隐藏敏感字段]'},redacted_fields:1},diagnostics:{...row.diagnostics,code:count?'':'admin_cancelled',reason:count?'等待模型响应':'管理员中断'}}
    await route.fulfill({json:data})
  })
  await page.goto('http://127.0.0.1:5175/usage')
  const panel=page.getByRole('region',{name:'在途请求与并发占用'})
  const bar=panel.getByRole('progressbar',{name:'qwen3.8-27b 并发占用'})
  await expect(bar).toHaveAttribute('aria-valuenow','4');await expect(panel).toContainText('已满')
  await expect(panel.getByRole('row')).toHaveCount(5)
  const evidence=path.resolve(process.cwd(),'../../../.local/request-cancel-browser');fs.mkdirSync(evidence,{recursive:true})
  await page.screenshot({path:path.join(evidence,'active-four.png')})
  const first=panel.getByRole('row').filter({hasText:'ui-monitor-t'}).first()
  await first.getByRole('button',{name:'查看内容与详情'}).click()
  const detail=page.getByRole('dialog',{name:'请求详情'})
  await expect(detail).toContainText('max_tokens');await expect(detail).toContainText('1 个敏感字段已隐藏')
  await expect(detail).toContainText('172.18.0.7');await expect(detail).toContainText('非流式，等待完整响应')
  await page.screenshot({path:path.join(evidence,'active-detail.png')})
  page.once('dialog',d=>d.dismiss())
  await detail.getByRole('button',{name:'中断请求',exact:true}).click()
  expect(cancelPosts).toBe(0)
  page.once('dialog',d=>d.accept())
  await detail.getByRole('button',{name:'中断请求',exact:true}).click()
  await expect(detail.getByRole('button',{name:'中断处理中…',exact:true})).toBeDisabled()
  expect(cancelPosts).toBe(1)
  await expect(bar).toHaveAttribute('aria-valuenow','4')
  count=0
  await expect(detail).toContainText('管理员中断',{timeout:8000})
  await page.keyboard.press('Escape');await expect(detail).not.toBeVisible()
  await expect(panel).toContainText('当前没有在途请求')
  await page.getByRole('checkbox',{name:'每 5 秒刷新'}).uncheck()
  count=3;await panel.getByRole('button',{name:'刷新在途'}).click()
  await expect(bar).toHaveAttribute('aria-valuenow','3')
  fail=true;await panel.getByRole('button',{name:'刷新在途'}).click()
  await expect(panel.getByRole('alert')).toContainText('上次成功快照')
  await expect(bar).toHaveAttribute('aria-valuenow','3')
  await page.setViewportSize({width:390,height:844})
  await panel.getByRole('heading').scrollIntoViewIfNeeded()
  await page.screenshot({path:path.join(evidence,'active-mobile.png')})
  expect(errors).toEqual([])
})
