import { expect, test } from '@playwright/test'

// Deterministic UI contract tests. No Docker, hardware, credentials or remote device changes.
test.beforeEach(async ({ page }) => {
  const model = { id:'mobilenet_v2',capability:'cv_classification',device:'CIX-NPU',version:'1',modes:[],image:'cix-cv:0.1.0',download_bytes:1024,cached_bytes:0,downloaded:false,installed:false,enabled:false,running:false,status:'not_installed',image_ready:true,mode_compatible:true,rollback_available:false,sources:['aistore/mobilenet_v2.cix'] }
  const components = ['nginx','etcd','mysql','redis','paradedb','neo4j','minio','milvus'].map(id=>({id,name:id==='redis'?'Redis':id,status:'not_installed',running:false,managed:false,healthy:null,image:'',ports:[6379]}))
  const jobs: any[] = []; const uploads: Record<string,any> = {}
  function job(kind: string, target: string) { const value={id:(jobs.length+1).toString().padStart(32,'0'),kind,target,status:'succeeded',created_at:'2026-10-15T08:00:00Z',result:{message:'操作完成'}};jobs.unshift(value);return value }
  page.on('dialog',d=>d.accept())
  await page.route('**/api/**', async route => {
    const request=route.request(), path=new URL(request.url()).pathname, body=request.method()==='POST'?request.postDataJSON():{}
    let value: any={}
    if(path==='/api/session')value={authenticated:true,csrf:'fixture-csrf',features:['all']}
    else if(path==='/api/overview')value={gateway:{status:'ok',active_mode:'copaw'},host:{system:{},containers:[],accelerators:[],platform:{allow_mode_switch:true}},usage:{},models:{data:[]}}
    else if(path==='/api/models')value={data:[]}
    else if(path==='/api/ops/overview')value={version:'1.2.0',device:{at:1792051200,cpu_percent:12,memory_percent:27,temperature_c:40,hostname:'AI-BOX-120',uptime_seconds:36000,disks:[{path:'/data',total:1024**4,used:512*1024**3,free:512*1024**3,percent:50}]},probe:{running:true,registered:true,mqtt_connected:true,device_id:'BOX-fixture',platform:'http://aistore.example',checked_at:1792051200,last_report_at:null},history:[{at:1792051170,cpu_percent:10,memory_percent:25},{at:1792051200,cpu_percent:12,memory_percent:27}],alerts:[],components}
    else if(path==='/api/ops/tasks')value={data:jobs}
    else if(path==='/api/ops/components/plan')value={valid:true,selected:body.ids,dependencies:[],steps:['检查依赖','健康检查']}
    else if(path==='/api/ops/components/install'){for(const row of components)if(body.ids.includes(row.id))Object.assign(row,{managed:true,running:true,status:'healthy'});value=job('component-install',body.ids.join(','))}
    else if(path==='/api/ops/components/action'){const row=components.find(r=>r.id===body.id)!;row.running=body.action!=='stop';row.status=row.running?'healthy':'stopped';value=job('component-'+body.action,body.id)}
    else if(path==='/api/ops/components/logs')value={lines:['component ready']}
    else if(path==='/api/model-install')value={models:[model],mode:'copaw',managed:true,storage_free_bytes:100*1024**3,uploads:Object.values(uploads)}
    else if(path==='/api/model-install/plan')value={valid:true,errors:[],notices:[],download_bytes:model.downloaded?0:1024,required_free_bytes:1024,steps:['校验','安装','自测']}
    else if(path==='/api/model-install/actions/download'){Object.assign(model,{downloaded:true,cached_bytes:1024});value=job('model-download',model.id)}
    else if(path==='/api/model-install/actions/install'){Object.assign(model,{downloaded:true,cached_bytes:1024,installed:true,enabled:body.activate,running:body.activate,status:body.activate?'ready':'installed'});value=job('model-install',model.id)}
    else if(path==='/api/model-install/local')value={path:'',entries:[{name:'model.cix',path:'model.cix',directory:false,bytes:1024}]}
    else if(path==='/api/model-install/local/import'){Object.assign(model,{downloaded:true,cached_bytes:1024});value=job('model-import','staging:model.cix')}
    else if(path==='/api/model-install/uploads'){value={id:'a'.repeat(32),name:body.name,size:body.size,offset:0,kind:body.kind};uploads[value.id]=value}
    else if(path.endsWith('/import')){Object.assign(model,{downloaded:true,cached_bytes:1024});value=job('model-import','upload')}
    else if(path.startsWith('/api/model-install/uploads/')){const id=path.split('/')[4];if(request.method()==='DELETE'){delete uploads[id];value={deleted:id}}else{uploads[id].offset+=request.postDataBuffer()?.length||0;value=uploads[id]}}
    else if(path.startsWith('/api/jobs/'))value=jobs.find(j=>j.id===path.split('/').pop())
    return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(value)})
  })
})

test('device overview, components install, stop and task history',async({page},info)=>{
  const errors: string[]=[];page.on('pageerror',e=>errors.push(e.message))
  await page.setViewportSize({width:1440,height:1050});await page.goto('/')
  await expect(page.getByRole('heading',{name:'AI-BOX-120'})).toBeVisible()
  await expect(page.getByText('AI STORE 已连接',{exact:true})).toBeVisible()
  await page.screenshot({path:info.outputPath('device-overview.png'),fullPage:true})
  await page.getByRole('link',{name:'组件中心',exact:true}).click()
  await page.getByLabel('补装 Redis',{exact:true}).check();await page.getByLabel('redis 密码',{exact:true}).fill('fixture-password-123')
  await page.getByRole('button',{name:'安装预检',exact:true}).click();await expect(page.getByRole('button',{name:'安装组件',exact:true})).toBeEnabled()
  await page.getByRole('button',{name:'安装组件',exact:true}).click()
  const row=page.getByRole('row').filter({has:page.getByLabel('补装 Redis',{exact:true})})
  await expect(row).toContainText('运行健康');await row.getByRole('button',{name:'停止',exact:true}).click();await expect(row).toContainText('已停止')
  await page.screenshot({path:info.outputPath('components.png'),fullPage:true})
  await page.getByRole('link',{name:'任务中心',exact:true}).click();await expect(page.getByText('component-stop · redis',{exact:true})).toBeVisible()
  expect(errors).toEqual([])
})

test('local import, download and supplement model installation',async({page},info)=>{
  const errors:string[]=[];page.on('pageerror',e=>errors.push(e.message))
  await page.goto('/model-install');await expect(page.getByRole('heading',{name:'按需安装已适配模型'})).toBeVisible()
  await page.getByRole('button',{name:'浏览',exact:true}).click();await page.getByRole('button',{name:'导入 · model.cix',exact:true}).click()
  const row=page.getByRole('row').filter({has:page.getByLabel('选择 mobilenet_v2',{exact:true})})
  await expect(row).toContainText('已缓存 · 待安装')
  await page.getByLabel('选择 mobilenet_v2',{exact:true}).check();await page.getByLabel('安装后立即启用并自测（可能重启共享服务）').check()
  await page.getByRole('button',{name:'安装预检',exact:true}).click();await page.getByRole('button',{name:'安装并启用',exact:true}).click()
  await expect(row).toContainText('已启用 · 查看运行诊断')
  await page.screenshot({path:info.outputPath('model-supplement.png'),fullPage:true})
  expect(errors).toEqual([])
})

test('browser raw file upload imports to cache and cleans uploaded copy',async({page})=>{
  await page.goto('/model-install');await page.getByLabel('上传离线交付包').setInputFiles({name:'model.cix',mimeType:'application/octet-stream',buffer:Buffer.from('fixture model')})
  await expect(page.getByText('文件已上传、校验并导入缓存。选择模型执行预检和安装即可；尚未启用服务。',{exact:true})).toBeVisible()
  await expect(page.getByRole('row').filter({has:page.getByLabel('选择 mobilenet_v2',{exact:true})})).toContainText('已缓存 · 待安装')
})
