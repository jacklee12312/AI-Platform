<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Download, RefreshCw, PackageCheck } from '@lucide/vue'
import { api, describeError } from '../api'
import { operations, operationState } from '../operations'

interface Model {
  id: string; capability: string; device: string; version: string; modes: string[]; image: string
  download_bytes: number; cached_bytes: number; downloaded: boolean; installed: boolean; enabled: boolean
  status: string; image_ready: boolean; mode_compatible: boolean; rollback_available: boolean
  last_probe?: { passed: boolean }
}
interface Upload { id: string; name: string; kind: string; size: number; offset: number }
interface Catalog { models: Model[]; mode: string; managed: boolean; storage_free_bytes: number; uploads: Upload[] }
interface Plan { valid: boolean; errors: string[]; notices: string[]; download_bytes: number; required_free_bytes: number; steps: string[] }
const catalog = ref<Catalog | null>(null)
const selected = ref<string[]>([])
const query = ref('')
const device = ref('all')
const activate = ref(false)
const plan = ref<Plan | null>(null)
const planSignature = ref('')
const loading = ref(false)
const uploading = ref(false)
const uploadKind = ref<'models' | 'runtime'>('models')
const uploadProgress = ref('')
const error = ref('')
const message = ref('')
const busy = computed(() => loading.value || uploading.value || operationState.busy)
const visible = computed(() => (catalog.value?.models || []).filter(m => m.id.toLowerCase().includes(query.value.toLowerCase()) && (device.value === 'all' || m.device === device.value)))
const signature = computed(() => JSON.stringify({ ids: selected.value, activate: activate.value }))
const currentPlan = computed(() => planSignature.value === signature.value ? plan.value : null)
function bytes(value: number) { return value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GiB` : `${(value / 1024 ** 2).toFixed(1)} MiB` }
function status(m: Model) {
  if (m.status === 'installing' || m.status === 'recovery_required') return '安装中断 · 待恢复'
  if (m.enabled) return m.mode_compatible ? (m.status === 'legacy' ? '已有部署 · 待安装校验' : '已启用 · 查看运行诊断') : '已启用 · 等待匹配模式'
  if (m.status === 'rolled_back') return '已回退 · 未启用'
  if (m.installed) return '已安装 · 未启用'
  return m.downloaded ? '已缓存 · 待安装' : '未安装'
}
async function refresh() {
  loading.value = true
  try { catalog.value = await api<Catalog>('/api/model-install') }
  catch (reason) { error.value = describeError(reason) }
  finally { loading.value = false }
}
async function preflight() {
  error.value = ''; plan.value = null; loading.value = true
  const body = signature.value
  try { plan.value = await api<Plan>('/api/model-install/plan', { method: 'POST', body }); planSignature.value = body }
  catch (reason) { error.value = describeError(reason) }
  finally { loading.value = false }
}
async function run(action: string, ids = selected.value) {
  if (!ids.length || busy.value) return
  if (action !== 'download' && !window.confirm(`${({ install: activate.value ? '安装并启用（会重启相关服务）' : '安装但不启用', activate: '重启服务并执行实际推理自测', disable: '停用模型', rollback: '恢复上次安装前的文件并保持停用' } as Record<string, string>)[action]}：${ids.join('、')}？共享 CV 服务的其他模型可能暂时不可用。`)) return
  error.value = ''; message.value = ''
  try {
    const job = await operations.submit('/api/model-install/actions/' + action, { method: 'POST', body: JSON.stringify({ ids, activate: activate.value }) }, `模型${({ download: '下载', install: '安装', activate: '启用', disable: '停用', rollback: '回退' } as Record<string, string>)[action]}`)
    message.value = String(job.result?.message || '操作已完成，请查看最新状态。')
  } catch (reason) { error.value = describeError(reason) }
  finally { plan.value = null; await refresh() }
}
async function upload(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploading.value = true; error.value = ''; message.value = ''
  try {
    const candidates = catalog.value?.uploads.filter(u => u.name === file.name && u.size === file.size && u.kind === uploadKind.value) || []
    let item = candidates[0]
    if (item && !window.confirm('发现同名同大小的上传记录。确认是同一文件并续传？如不是，请先删除旧上传包。')) return
    item = item ? await api<Upload>(`/api/model-install/uploads/${item.id}`) : await api<Upload>('/api/model-install/uploads', { method: 'POST', body: JSON.stringify({ name: file.name, size: file.size, kind: uploadKind.value }) })
    while (item.offset < file.size) {
      uploadProgress.value = `${file.name} · ${Math.floor(item.offset / file.size * 100)}% · ${bytes(item.offset)} / ${bytes(file.size)}`
      item = await api<Upload>(`/api/model-install/uploads/${item.id}?offset=${item.offset}`, { method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body: file.slice(item.offset, Math.min(item.offset + 8 * 1024 ** 2, file.size)) })
    }
    message.value = '上传完成。点击“校验导入”后，模型包进入缓存；镜像包只导入运行环境，不会启动服务。'
  } catch (reason) { error.value = describeError(reason) }
  finally { uploading.value = false; uploadProgress.value = ''; input.value = ''; await refresh() }
}
async function importFile(item: Upload) {
  if (item.kind === 'runtime' && !window.confirm('仅导入可信的本项目 ARM64 Docker save 镜像包，可能更新同名运行镜像。确认来源可信？')) return
  error.value = ''; message.value = ''
  try {
    await operations.submit(`/api/model-install/uploads/${item.id}/import`, { method: 'POST' }, '校验导入离线包')
    message.value = '离线包导入完成。选择模型执行安装；确认无需再次导入后，可以删除上传包释放空间。'
  } catch (reason) { error.value = describeError(reason) }
  finally { plan.value = null; await refresh() }
}
async function deleteFile(item: Upload) {
  if (!window.confirm(`删除上传包 ${item.name}？不能恢复，但不会删除已导入缓存或已安装模型。`)) return
  loading.value = true
  try { await api(`/api/model-install/uploads/${item.id}`, { method: 'DELETE' }); message.value = '上传包已删除；模型缓存及已安装文件保留。' }
  catch (reason) { error.value = describeError(reason) }
  finally { await refresh() }
}
onMounted(refresh)
</script>

<template>
  <section class="page-body">
    <div v-if="error" class="notice error-notice" role="alert">{{ error }}</div>
    <div v-if="message" class="notice success-notice" role="status">{{ message }}</div>
    <section class="panel install-intro">
      <div><h2><PackageCheck :size="20" />按需安装已适配模型</h2><p>选择模型 → 下载或离线导入 → 安装预检 → 安装 → 启用自测。权重与运行环境分开管理，未通过自测不会开放调用。</p></div>
      <p v-if="catalog">当前模式 <code>{{ catalog.mode }}</code> · 可用空间 {{ bytes(catalog.storage_free_bytes) }} · {{ catalog.models.length }} 个已适配模型</p>
      <p>第一版仅适用于已部署 ModelDeck 基础平台的 Linux ARM64 设备。运行镜像、驱动由平台包提供；这里不自动安装系统驱动。后摩互斥模式需在“模型管理”切换。</p>
      <p v-if="catalog && !catalog.managed">已有全量部署尚未接管。首次安装/启停时会记录现有模型；之后重启与模式切换只加载已安装且已启用的后摩模型。</p>
    </section>

    <section class="panel">
      <div class="panel-head"><div><h2>离线交付包</h2><p>模型包使用配套命令行工具生成；运行环境包使用 Docker save 导出。上传分块续传，导入后仍需选择模型安装。</p></div></div>
      <div class="upload-controls"><select v-model="uploadKind" :disabled="busy" aria-label="离线包类型"><option value="models">模型权重包</option><option value="runtime">运行环境镜像包</option></select><input type="file" accept=".tar,.gz,.tgz" :disabled="busy" aria-label="上传离线交付包" @change="upload" /><span role="status">{{ uploadProgress }}</span></div>
      <div v-for="item in catalog?.uploads" :key="item.id" class="upload-row"><span><strong>{{ item.name }}</strong><small>{{ item.kind === 'runtime' ? '运行环境' : '模型权重' }} · {{ bytes(item.offset) }} / {{ bytes(item.size) }}</small></span><button class="secondary-button" :disabled="busy || item.offset !== item.size" @click="importFile(item)">校验导入</button><button class="secondary-button" :disabled="busy" @click="deleteFile(item)">删除上传包</button></div>
    </section>

    <section class="page-toolbar panel">
      <input v-model="query" aria-label="搜索可安装模型" placeholder="搜索模型 ID" />
      <select v-model="device" aria-label="按设备筛选"><option value="all">全部设备</option><option v-for="name in [...new Set(catalog?.models.map(m => m.device) || [])]" :key="name">{{ name }}</option></select>
      <button class="secondary-button" :disabled="busy" @click="refresh"><RefreshCw :size="15" />刷新安装状态</button>
    </section>

    <section class="panel full-table-panel">
      <div class="panel-head"><div><h2>模型安装目录</h2><p>下载会校验固定 SHA-256；CPU 模型的词典、分词器、音色等依赖也包含在内。自测验证执行链路，不代替业务效果验收。</p></div></div>
      <div class="table-wrap"><table class="management-table"><thead><tr><th>选择</th><th>模型 / 设备</th><th>文件 / 运行环境</th><th>安装状态</th><th>操作</th></tr></thead><tbody>
        <tr v-for="m in visible" :key="m.id"><td><input v-model="selected" type="checkbox" :value="m.id" :disabled="busy" :aria-label="`选择 ${m.id}`" /></td><td><strong>{{ m.id }}</strong><small>{{ m.device }} · {{ m.capability }}</small><small>{{ m.modes.join(' / ') || '不限模式' }}</small></td><td>{{ bytes(m.download_bytes) }}<small>缓存 {{ bytes(m.cached_bytes) }}</small><small :class="{ warning: !m.image_ready }">{{ m.image_ready ? '运行环境已就绪' : `缺少镜像 ${m.image}` }}</small></td><td><span :class="['status-pill', m.enabled ? 'status-online' : 'status-inactive']">{{ status(m) }}</span></td><td><div class="row-actions"><button class="secondary-button" :disabled="busy || !m.installed || !m.mode_compatible || !m.image_ready" @click="run('activate', [m.id])">启用自测</button><button class="secondary-button" :disabled="busy || !m.enabled" @click="run('disable', [m.id])">停用</button><button class="secondary-button" :disabled="busy || !m.rollback_available" @click="run('rollback', [m.id])">恢复旧文件</button></div></td></tr>
        <tr v-if="!visible.length"><td colspan="5" class="empty-row">{{ loading ? '正在读取安装状态…' : '没有匹配的模型' }}</td></tr>
      </tbody></table></div>
    </section>

    <section class="panel install-actions">
      <strong>已选 {{ selected.length }} 个模型</strong>
      <label><input v-model="activate" type="checkbox" :disabled="busy" />安装后立即启用并自测（可能重启共享服务）</label>
      <div class="row-actions"><button class="secondary-button" :disabled="busy || !selected.length" @click="run('download')"><Download :size="15" />仅下载到缓存</button><button class="secondary-button" :disabled="busy || !selected.length" @click="preflight">安装预检</button><button class="primary-button" :disabled="busy || !currentPlan?.valid" @click="run('install')">{{ activate ? '安装并启用' : '安装但不启用' }}</button></div>
      <template v-if="currentPlan"><p>需下载 {{ bytes(currentPlan.download_bytes) }} · 预留空间 {{ bytes(currentPlan.required_free_bytes) }}</p><p>{{ currentPlan.steps.join(' → ') }}</p><p v-for="item in currentPlan.errors" :key="item" class="warning" role="alert">{{ item }}</p><p v-for="item in currentPlan.notices" :key="item" class="warning">{{ item }}</p><p v-if="currentPlan.valid">预检通过。执行前会再次检查模式、环境和可用空间。</p></template>
    </section>
  </section>
</template>

<style scoped>
.install-intro,.install-actions { padding:24px;display:grid;gap:14px; }
.management-table { min-width:900px; }
h2 { display:flex;align-items:center;gap:10px;font-size:16px; } p { font-size:13px;line-height:1.8;color:#64716b;margin:0; }
small { display:block;font-size:11px;line-height:1.7;margin-top:5px;color:#697870;overflow-wrap:anywhere; }
.row-actions,.upload-controls { display:flex;align-items:center;gap:8px;flex-wrap:wrap; }
.upload-controls,.upload-row { padding:16px 24px; }
.upload-row { display:flex;align-items:center;gap:12px;border-top:1px solid #edf1ef; } .upload-row span { flex:1;overflow-wrap:anywhere; }
.warning { color:#a15b22; } label { font-size:13px; } input[type=checkbox] { accent-color:#2e7a60; } .row-actions button { font-size:12px; } td { vertical-align:top; }
.upload-controls select,.page-toolbar>input { border:1px solid #d7e1dc;border-radius:8px;padding:9px 12px;font:inherit;font-size:13px;background:white; }
.upload-controls input[type=file] { font:inherit;font-size:13px;max-width:100%; }
.upload-controls input::file-selector-button { padding:8px 12px;border:1px solid #d7e1dc;border-radius:8px;background:#f6faf8;margin-right:10px;cursor:pointer; }
</style>
