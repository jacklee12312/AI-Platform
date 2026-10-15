<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Download, Files, RefreshCw, Upload, X } from '@lucide/vue'
import { api, ApiError, describeError, getCsrf } from '../api'

type Kind = 'ocr' | 'asr'
interface Item { id: string; filename: string; status: string; size: number; pages: number; completed_pages: number; stage: string; error: string; warning: string; has_result: boolean; request_id: string }
interface Job { id: string; name: string; kind: Kind; status: string; created_at: number; total: number; completed: number; progress: number; queue_position: number; counts: Record<string, number>; items?: Item[] }
interface Listing { data: Job[]; storage_bytes: number; storage_limit_bytes: number }
interface Selected { id: string; file: File; loaded: number; ready: boolean }
const labels: Record<string, string> = { draft: '待提交', uploading: '上传中', ready: '已上传', queued: '排队中', running: '处理中', waiting: '等待资源', cancelling: '取消中', succeeded: '已完成', partial: '部分成功', failed: '失败', cancelled: '已取消', interrupted: '执行中断' }
const terminal = new Set(['succeeded', 'partial', 'failed', 'cancelled', 'interrupted'])
const kind = ref<Kind>('ocr'), name = ref(''), language = ref('auto')
const files = ref<Selected[]>([]), uploading = ref(false), draftId = ref(''), uploadStage = ref('')
const listing = ref<Listing>({ data: [], storage_bytes: 0, storage_limit_bytes: 2 * 1024 ** 3 })
const selectedId = ref(''), detail = ref<Job | null>(null), filter = ref('all')
const error = ref(''), pollError = ref(''), notice = ref(''), busy = ref(''), preview = ref<{ filename: string; text: string; status: string; warning: string } | null>(null)
let timer: ReturnType<typeof setInterval> | undefined, xhr: XMLHttpRequest | null = null, stopped = false, refreshing = false, cancelUpload = false
const totalBytes = computed(() => files.value.reduce((n, item) => n + item.file.size, 0))
const uploadedBytes = computed(() => files.value.reduce((n, item) => n + item.loaded, 0))
const jobs = computed(() => listing.value.data.filter(job => filter.value === 'all' || job.kind === filter.value))
const activeCount = computed(() => listing.value.data.filter(job => ['queued', 'running', 'waiting', 'cancelling'].includes(job.status)).length)
const accept = computed(() => kind.value === 'ocr' ? '.png,.jpg,.jpeg,.webp,.pdf' : '.wav,.mp3,.m4a,.flac,.ogg,.aac')
function bytes(n: number) { return `${(n / 1024 ** 2).toFixed(1)} MB` }
function date(n: number) { return new Date(n * 1000).toLocaleString('zh-CN', { hour12: false }) }
function uuid() {
  // LAN HTTP pages do not expose crypto.randomUUID in all browsers.
  const b = crypto.getRandomValues(new Uint8Array(16)); b[6] = (b[6]! & 15) | 64; b[8] = (b[8]! & 63) | 128
  return Array.from(b, v => v.toString(16).padStart(2, '0')).join('')
}
function choose(event: Event) {
  const input = event.target as HTMLInputElement
  const added = Array.from(input.files || []).map(file => ({ id: uuid(), file, loaded: 0, ready: false }))
  files.value.push(...added); input.value = ''; error.value = ''
}
function resetForm() { files.value = []; draftId.value = ''; name.value = ''; uploadStage.value = '' }
async function refresh() {
  if (refreshing || stopped) return
  refreshing = true
  try {
    listing.value = await api<Listing>('/api/batches')
    if (selectedId.value) {
      const id = selectedId.value
      const job = await api<Job>(`/api/batches/${id}`)
      if (selectedId.value === id) detail.value = job
    }
    pollError.value = ''
  } catch (err) { pollError.value = `进度刷新失败：${describeError(err)}。恢复连接后自动刷新。` }
  finally { refreshing = false }
}
async function select(job: Job) { selectedId.value = job.id; preview.value = null; await refresh() }
function uploadFile(jobId: string, item: Selected) {
  return new Promise<void>((resolve, reject) => {
    const request = new XMLHttpRequest(); xhr = request
    request.open('PUT', `/api/batches/${jobId}/files/${item.id}?filename=${encodeURIComponent(item.file.name)}&size=${item.file.size}`)
    request.withCredentials = true; request.timeout = 180000
    request.setRequestHeader('Content-Type', 'application/octet-stream'); request.setRequestHeader('X-CSRF-Token', getCsrf())
    request.upload.onprogress = event => { item.loaded = Math.min(event.loaded, item.file.size) }
    request.onload = () => {
      if (request.status >= 200 && request.status < 300) { item.ready = true; item.loaded = item.file.size; resolve() }
      else {
        let message: unknown = '文件上传失败'
        try { message = JSON.parse(request.responseText).detail } catch { /* retain fallback */ }
        reject(new ApiError(request.status, message))
      }
    }
    request.onerror = () => reject(new Error('上传连接中断；可重试未完成的上传'))
    request.ontimeout = () => reject(new Error('上传超时；可重试未完成的上传'))
    request.onabort = () => reject(new Error('已停止上传'))
    request.send(item.file)
  })
}
async function submitFiles() {
  error.value = ''; notice.value = ''; cancelUpload = false
  if (!files.value.length || files.value.length > 50 || totalBytes.value > 250 * 1024 ** 2) { error.value = '请选择 1–50 个文件，合计不超过 250 MB'; return }
  for (const item of files.value) {
    const ext = '.' + item.file.name.split('.').pop()?.toLowerCase()
    const limit = (kind.value === 'asr' ? 50 : ext === '.pdf' ? 40 : 10) * 1024 ** 2
    if (!accept.value.split(',').includes(ext) || !item.file.size || item.file.size > limit) { error.value = `${item.file.name}：格式不支持、文件为空或超过单文件限制`; return }
  }
  uploading.value = true
  if (!draftId.value) { draftId.value = uuid(); name.value = name.value.trim() || `${kind.value.toUpperCase()} · ${new Date().toLocaleString('zh-CN')}` }
  const id = draftId.value
  try {
    uploadStage.value = '创建任务'
    await api('/api/batches', { method: 'POST', body: JSON.stringify({ id, name: name.value, kind: kind.value, language: language.value }) })
    selectedId.value = id
    for (const [index, item] of files.value.entries()) {
      if (cancelUpload) break
      if (item.ready) continue
      uploadStage.value = `上传 ${index + 1}/${files.value.length} · ${item.file.name}`
      await uploadFile(id, item)
    }
    if (cancelUpload) return
    uploadStage.value = '加入处理队列'
    await api(`/api/batches/${id}/submit`, { method: 'POST' })
    notice.value = '已加入队列。关闭页面后，盒子会继续处理。'; resetForm()
  } catch (err) {
    if (!cancelUpload) error.value = describeError(err)
  } finally {
    xhr = null; uploading.value = false
    if (cancelUpload) {
      try { await api(`/api/batches/${id}/cancel`, { method: 'POST' }); resetForm(); notice.value = '已停止上传并取消任务' }
      catch (err) { error.value = `取消未确认：${describeError(err)}，请在任务列表核查状态` }
    }
    await refresh()
  }
}
function stopUpload() { cancelUpload = true; xhr?.abort() }
async function action(job: Job, operation: 'submit' | 'cancel' | 'delete') {
  if (operation === 'delete' && !window.confirm(`删除“${job.name}”及原文件、结果？请先下载需要保留的结果。`)) return
  busy.value = job.id; error.value = ''; notice.value = ''
  try {
    await api(`/api/batches/${job.id}${operation === 'delete' ? '' : '/' + operation}`, { method: operation === 'delete' ? 'DELETE' : 'POST' })
    if (operation === 'delete' && selectedId.value === job.id) { selectedId.value = ''; detail.value = null; preview.value = null }
    if (job.id === draftId.value && operation !== 'cancel') resetForm()
    notice.value = operation === 'cancel' ? '取消已提交。正在进行的单次推理结束后收尾，保留已完成结果。' : operation === 'delete' ? '任务及文件已删除' : '已加入队列'
    await refresh()
  } catch (err) { error.value = describeError(err) }
  finally { busy.value = '' }
}
async function showResult(item: Item) {
  try {
    const result = await api<{ filename: string; status: string; warning: string; result: { text?: string } }>(`/api/batches/${selectedId.value}/files/${item.id}/result`)
    preview.value = { ...result, text: result.result.text || '（识别结果为空）' }
  } catch (err) { error.value = describeError(err) }
}
onMounted(() => { refresh(); timer = setInterval(() => { if (!document.hidden) refresh() }, 2000) })
onUnmounted(() => { stopped = true; clearInterval(timer); xhr?.abort() })
</script>

<template>
  <div class="batch-page">
    <div class="batch-intro"><div><h2>一批文件，一次提交</h2><p>图片 / PDF 文字识别 · 音频转写 · 后台排队 · 结果打包</p></div><span>{{ activeCount }} 批处理中</span></div>
    <div v-if="error" class="batch-alert error" role="alert">{{ error }}</div>
    <div v-if="pollError" class="batch-alert error" role="alert">{{ pollError }}</div>
    <div v-if="notice" class="batch-alert" role="status">{{ notice }}</div>
    <section class="panel batch-create" aria-label="新建批量任务">
      <div class="batch-section-head"><h2>新建任务</h2><small>OCR、ASR 各 1 个处理队列</small></div>
      <div class="batch-form">
        <label>任务类型<select v-model="kind" :disabled="!!draftId || uploading || files.length > 0"><option value="ocr">OCR · 图片与 PDF</option><option value="asr">ASR · 音频转写</option></select></label>
        <label class="batch-name">任务名称<input v-model="name" maxlength="80" :disabled="!!draftId || uploading" placeholder="例如：9 月采购单 / 会议录音" /></label>
        <label v-if="kind === 'asr'">识别语言<select v-model="language" :disabled="!!draftId || uploading"><option value="auto">自动检测</option><option value="zh">中文</option><option value="en">英语</option><option value="ja">日语</option><option value="ko">韩语</option><option value="yue">粤语</option></select></label>
      </div>
      <label class="batch-drop" :class="{ disabled: uploading || !!draftId }"><Upload :size="24" /><strong>选择多个{{ kind === 'ocr' ? '图片或 PDF' : '音频文件' }}</strong><span>{{ kind === 'ocr' ? 'PNG、JPG、WebP、PDF' : 'WAV、MP3、M4A、FLAC、OGG、AAC' }}</span><input type="file" multiple :accept="accept" :disabled="uploading || !!draftId" aria-label="选择批量文件" @change="choose" /></label>
      <p class="batch-hint">每批最多 50 个文件 / 250 MB。{{ kind === 'ocr' ? '图片 ≤10 MB / 2000 万像素；PDF ≤40 MB / 50 页，每批合计 ≤200 页。' : '单文件 ≤50 MB / 30 分钟，自动转码后识别。' }}</p>
      <ul v-if="files.length" class="batch-selected"><li v-for="(item, index) in files" :key="item.id"><span>{{ item.file.name }}</span><small>{{ bytes(item.file.size) }} · {{ item.ready ? '已上传' : uploading ? Math.round(item.loaded / item.file.size * 100) + '%' : '待上传' }}</small><button v-if="!uploading && !draftId" :aria-label="`移除 ${item.file.name}`" @click="files.splice(index, 1)"><X :size="15" /></button></li></ul>
      <div v-if="uploading" class="batch-upload-progress"><p>{{ uploadStage }}</p><progress :value="uploadedBytes" :max="totalBytes || 1" aria-label="上传进度"></progress></div>
      <div class="batch-form-foot"><small>{{ files.length }} 个文件 · {{ bytes(totalBytes) }}</small><div><button v-if="uploading" class="batch-button" @click="stopUpload">停止上传</button><button v-else-if="draftId" class="batch-button" @click="resetForm">保留草稿，另建任务</button><button class="batch-button primary" :disabled="uploading || !files.length" @click="submitFiles"><Upload :size="15" />{{ uploading ? '正在上传…' : draftId ? '重试上传并提交' : '上传并加入队列' }}</button></div></div>
      <p class="batch-hint">由管理员提交，遵守网关的全局、模型和资源池限制，不计入客户 Key 配额。取消会停止后续文件与页面，当前单次推理结束后收尾。</p>
    </section>
    <section class="panel" aria-label="批量任务列表">
      <div class="batch-section-head"><h2>任务记录</h2><div class="batch-tools"><select v-model="filter" aria-label="筛选任务类型"><option value="all">全部类型</option><option value="ocr">OCR</option><option value="asr">ASR</option></select><button class="batch-button" @click="refresh"><RefreshCw :size="14" />刷新</button></div></div>
      <div class="batch-storage"><span>原文件存储 {{ bytes(listing.storage_bytes) }} / {{ bytes(listing.storage_limit_bytes) }}</span><span>下载后可删除任务以释放空间，最多保留 200 批。</span></div>
      <div v-if="!jobs.length" class="batch-empty"><Files :size="30" /><p>暂无任务，选择一批文件开始处理</p></div>
      <div v-else class="table-wrap"><table class="batch-table"><thead><tr><th>任务</th><th>状态 / 进度</th><th>结果</th><th>操作</th></tr></thead><tbody>
        <tr v-for="job in jobs" :key="job.id" :class="{ selected: selectedId === job.id }" :data-job-id="job.id">
          <td><button class="batch-title" @click="select(job)">{{ job.name }}</button><small>{{ job.kind.toUpperCase() }} · {{ date(job.created_at) }}</small></td>
          <td><span class="batch-badge" :data-state="job.status">{{ labels[job.status] || job.status }}</span><small v-if="job.queue_position > 1">同类队列第 {{ job.queue_position }} 批</small><progress :value="job.completed" :max="job.total || 1" :aria-label="`${job.name}处理进度`"></progress><small>已结束 {{ job.completed }} / {{ job.total }} 个文件</small></td>
          <td><span>成功 {{ job.counts.succeeded || 0 }}</span><small v-if="job.counts.failed || job.counts.interrupted">失败 / 中断 {{ (job.counts.failed || 0) + (job.counts.interrupted || 0) }}</small><small v-if="job.counts.cancelled">取消 {{ job.counts.cancelled }}</small></td>
          <td><div class="batch-row-actions"><button @click="select(job)">详情</button><a :href="`/api/batches/${job.id}/results.zip`" :aria-label="`下载 ${job.name} 结果`"><Download :size="13" />结果 ZIP</a><button v-if="job.status === 'draft'" :disabled="busy === job.id || uploading" @click="action(job, 'submit')">提交已上传文件</button><button v-if="!terminal.has(job.status)" :disabled="busy === job.id || job.status === 'cancelling' || uploading && job.id === draftId" @click="action(job, 'cancel')">取消</button><button v-if="terminal.has(job.status) || job.status === 'draft'" :disabled="busy === job.id || uploading && job.id === draftId" class="danger" @click="action(job, 'delete')">删除</button></div></td>
        </tr>
      </tbody></table></div>
    </section>
    <section v-if="detail" class="panel" aria-label="任务详情">
      <div class="batch-section-head"><div><h2>{{ detail.name }}</h2><small>{{ detail.kind.toUpperCase() }} · {{ labels[detail.status] }} · {{ detail.id.slice(0, 8) }}</small></div><button class="batch-button" @click="selectedId = ''; detail = null; preview = null">收起详情</button></div>
      <p v-if="detail.status === 'draft'" class="batch-detail-hint">尚未加入队列。可提交已上传文件，或删除此草稿后重新选择文件。</p>
      <p v-if="detail.status === 'cancelling'" class="batch-detail-hint">正在等待当前单次推理结束；后续文件与页面不会继续处理。</p>
      <p v-if="detail.status === 'interrupted'" class="batch-detail-hint" role="alert">部分已提交请求的最终结果未知，未自动重试。请先核查下方 Request ID；已保存的部分结果仍可下载。</p>
      <div class="table-wrap"><table class="batch-table"><thead><tr><th>文件</th><th>状态 / 阶段</th><th>结果 / 提示</th></tr></thead><tbody><tr v-for="item in detail.items" :key="item.id"><td><strong>{{ item.filename }}</strong><small>{{ bytes(item.size) }}</small></td><td><span class="batch-badge" :data-state="item.status">{{ labels[item.status] }}</span><small v-if="item.pages">已识别 {{ item.completed_pages }} / {{ item.pages }} 页</small><small>{{ item.stage }}</small></td><td><button v-if="item.has_result" class="batch-button" @click="showResult(item)">查看文本</button><p v-if="item.warning" class="batch-warning">{{ item.warning }}</p><p v-if="item.error" class="batch-error">{{ item.error }}</p><small v-if="item.request_id" class="batch-request">{{ item.request_id }}</small></td></tr></tbody></table></div>
      <div v-if="preview" class="batch-preview"><div><strong>{{ preview.filename }}</strong><button class="batch-button" @click="preview = null">关闭文本</button></div><p class="batch-hint">{{ labels[preview.status] }}{{ preview.warning ? ' · ' + preview.warning : '' }} · 未完成文件仅展示已保存的部分内容</p><pre>{{ preview.text }}</pre></div>
      <p class="batch-detail-hint">ZIP 包含逐文件 TXT / JSON、汇总 CSV 和任务清单；未完成、失败和取消状态会保留在清单中。</p>
    </section>
  </div>
</template>

<style scoped>
.batch-page{display:grid;gap:18px}.batch-intro{display:flex;justify-content:space-between;align-items:center;gap:16px}.batch-intro h2{font-size:21px}.batch-intro p{font-size:12px;color:var(--muted);margin-top:8px}.batch-intro>span{background:#e0f4eb;color:#267258;padding:9px 13px;border-radius:20px;font-size:12px;white-space:nowrap}.batch-section-head{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:18px 20px;border-bottom:1px solid var(--line)}small,.batch-hint{font-size:12px;color:var(--muted);line-height:1.65}.batch-section-head small{display:block;margin-top:4px}.batch-create{padding-bottom:16px}.batch-form{display:flex;gap:14px;flex-wrap:wrap;padding:18px 20px}.batch-form label{display:grid;gap:7px;font-size:12px;font-weight:600}.batch-name{flex:1;min-width:180px}input,select{border:1px solid var(--line);border-radius:7px;padding:9px 10px;background:white;min-width:0;color:var(--ink)}.batch-drop{margin:0 20px;padding:23px;border:1px dashed #9ac4b9;border-radius:9px;background:#f7fbf9;display:flex;align-items:center;justify-content:center;gap:12px;flex-wrap:wrap;position:relative;color:#2c6555;cursor:pointer}.batch-drop strong{font-size:14px}.batch-drop span{font-size:12px;color:var(--muted)}.batch-drop input{position:absolute;inset:0;width:100%;height:100%;opacity:0;cursor:pointer}.batch-drop.disabled{opacity:.5;cursor:default}.batch-hint{margin:10px 20px 0}.batch-selected{list-style:none;padding:0 20px;margin:12px 0;max-height:180px;overflow:auto}.batch-selected li{display:flex;gap:10px;align-items:center;padding:8px 0;border-bottom:1px solid #edf1ef;font-size:12px}.batch-selected li>span{flex:1;overflow-wrap:anywhere}.batch-selected button{border:0;background:none;color:var(--muted);padding:5px}.batch-form-foot{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:14px 20px}.batch-form-foot>div,.batch-tools{display:flex;gap:8px;flex-wrap:wrap}.batch-button{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:1px solid var(--line);background:white;padding:8px 11px;border-radius:7px;color:#426057;font-size:12px}.batch-button.primary{background:#173d34;color:white;border-color:#173d34}button:disabled{opacity:.45;cursor:not-allowed}.batch-alert{background:#e8f7ef;border:1px solid #c2e3cf;color:#266c48;padding:12px 16px;border-radius:8px;font-size:13px;line-height:1.6}.batch-alert.error{background:#fff1ee;border-color:#edc9c1;color:#ae483b}.batch-upload-progress{padding:8px 20px;font-size:12px}.batch-upload-progress p{margin-bottom:8px;overflow-wrap:anywhere}progress{display:block;width:100%;height:6px;border:0;border-radius:6px;overflow:hidden;appearance:none;background:#e7eeeb;accent-color:#19a974;margin:9px 0 5px}progress::-webkit-progress-bar{background:#e7eeeb}progress::-webkit-progress-value{background:#19a974;border-radius:6px}.batch-storage{padding:12px 20px;display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;font-size:11px;color:var(--muted)}.batch-empty{padding:42px;display:grid;place-items:center;gap:13px;color:var(--muted);font-size:13px}.batch-table{min-width:700px;table-layout:fixed}.batch-table th{font-size:11px;padding:11px 20px}.batch-table td{padding:17px 20px;vertical-align:top;font-size:12px;overflow-wrap:anywhere}.batch-table th:first-child{width:30%}.batch-table th:last-child{width:29%}.batch-table small{display:block;font-size:11px;margin-top:4px}.batch-table tr.selected{background:#f4faf7}.batch-title{border:0;background:none;padding:0;color:#244a3e;font-weight:650;text-align:left;line-height:1.7}.batch-badge{display:inline-block;padding:3px 7px;border-radius:5px;background:#edf1f3;color:#637781;font-size:11px}.batch-badge[data-state=succeeded]{background:#e1f4e9;color:#1c8557}.batch-badge[data-state=running]{background:#e2f2f7;color:#19758c}.batch-badge[data-state=waiting],.batch-badge[data-state=partial],.batch-badge[data-state=cancelling]{background:#fff1d8;color:#986218}.batch-badge[data-state=failed],.batch-badge[data-state=interrupted]{background:#fbe7e2;color:#a34c3c}.batch-row-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.batch-row-actions button,.batch-row-actions a{display:inline-flex;align-items:center;gap:4px;border:0;background:none;padding:2px 0;color:#24775d;text-decoration:none;font-size:12px}.batch-row-actions .danger{color:#a55447}.batch-detail-hint{padding:13px 20px;font-size:12px;color:var(--muted);line-height:1.7}.batch-warning{color:#986218;font-size:12px;margin-top:7px;line-height:1.6}.batch-error{color:#ad4a3a;font-size:12px;line-height:1.6}.batch-request{font-family:Consolas,monospace;font-size:10px!important;word-break:break-all}.batch-preview{margin:16px 20px;padding:15px;border:1px solid var(--line);border-radius:8px;background:#f8faf9}.batch-preview>div{display:flex;justify-content:space-between;align-items:center;gap:12px;font-size:13px}.batch-preview .batch-hint{margin:10px 0}.batch-preview pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:420px;overflow:auto;font:13px/1.8 Consolas,'Microsoft YaHei',monospace}.batch-button:focus-visible,.batch-title:focus-visible{outline:2px solid #19a974;outline-offset:3px}@media(max-width:700px){.batch-form{display:grid}.batch-intro{align-items:flex-start}.batch-intro h2{font-size:18px}.batch-form-foot{align-items:flex-start;flex-direction:column}.batch-drop{flex-direction:column}.batch-section-head{flex-wrap:wrap}}
</style>
