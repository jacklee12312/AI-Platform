<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { Copy, Download, LoaderCircle, Play, RefreshCw, Square, TriangleAlert } from '@lucide/vue'
import { ApiError, describeError, getCsrf } from '../api'
import { models, overview, overviewError, refreshOverview } from '../store'
import DetectionPreview from '../components/DetectionPreview.vue'
import { cvView, experiences, matchesExperience, prepareImage, readResponse, seconds, streamChat, transcriptView,
  type ExperienceKind, type PreparedImage } from '../experience'
import { newEvaluationId } from '../evaluations'

const route = useRoute()
const availableExperiences = computed(() => overview.value?.host.platform?.id === 'ascend-native'
  ? experiences.filter(item => models.value.data.some(model => matchesExperience(model, item.id))) : experiences)
const kind = ref<ExperienceKind>('chat'), model = ref('')
const prompt = ref('请从 1 数到 30，用空格分隔，不要解释。'), stream = ref(true)
const maxTokens = ref(256), temperature = ref(.2), confidence = ref(.25), iou = ref(.45), speed = ref(1), language = ref('auto')
const running = ref(false), preparing = ref(false), error = ref(''), notice = ref(''), copied = ref(false)
const image = ref<PreparedImage | null>(null), audioFile = ref<File | null>(null), audioUrl = ref(''), speechUrl = ref('')
const output = ref(''), rawOutput = ref(''), rawValue = ref<unknown>(null), done = ref(false)
const inputAudio = ref<HTMLAudioElement | null>(null), activeTime = ref(0), previewAudioError = ref('')
const requestId = ref(''), resultModel = ref(''), resultKind = ref<ExperienceKind>('chat'), sentConfidence = ref(.25)
const metrics = ref<{ status: number | null; headers: number | null; total: number | null; first: number | null; chunks: number }>({ status: null, headers: null, total: null, first: null, chunks: 0 })
let controller: AbortController | null = null, selectionVersion = 0, disposed = false
const choices = computed(() => models.value.data.filter(item => matchesExperience(item, kind.value)))
const selected = computed(() => choices.value.find(item => item.id === model.value))
const needsImage = computed(() => ['vision', 'ocr', 'cv'].includes(kind.value))
const isChat = computed(() => ['chat', 'vision', 'ocr'].includes(kind.value))
const canRun = computed(() => !running.value && !preparing.value && !overviewError.value && selected.value?.status === 'online'
  && (!needsImage.value || image.value) && (kind.value !== 'asr' || audioFile.value)
  && (!['chat', 'vision', 'ocr', 'tts'].includes(kind.value) || prompt.value.trim())
  && (!isChat.value || Number.isInteger(maxTokens.value) && maxTokens.value >= 1 && maxTokens.value <= 2048 && Number.isFinite(temperature.value) && temperature.value >= 0 && temperature.value <= 2)
  && (kind.value !== 'tts' || prompt.value.length <= 500 && speed.value >= .5 && speed.value <= 2)
  && (kind.value !== 'cv' || Number.isFinite(confidence.value) && confidence.value >= 0 && confidence.value <= 1 && Number.isFinite(iou.value) && iou.value >= 0 && iou.value <= 1))
const cv = computed(() => rawValue.value && image.value && resultKind.value === 'cv' ? cvView(rawValue.value, resultModel.value, image.value.width, image.value.height) : null)
const transcript = computed(() => { try { return resultKind.value === 'asr' && rawValue.value ? transcriptView(rawValue.value) : null } catch { return null } })
const defaults: Record<ExperienceKind, string> = { chat: '请从 1 数到 30，用空格分隔，不要解释。', vision: '请描述图片中的主要内容。', cv: '', ocr: '请识别图片中的文字，按阅读顺序输出，保留换行，不要解释。', asr: '', tts: '你好，欢迎使用 ModelDeck 模型体验中心。' }

function revoke(value: string) { if (value) URL.revokeObjectURL(value) }
function clearResult() {
  output.value = ''; rawOutput.value = ''; rawValue.value = null; done.value = false; error.value = ''; notice.value = ''; copied.value = false
  revoke(speechUrl.value); speechUrl.value = ''; requestId.value = ''; resultModel.value = ''; previewAudioError.value = ''
  metrics.value = { status: null, headers: null, total: null, first: null, chunks: 0 }
}
function clearMedia() {
  selectionVersion++; preparing.value = false
  revoke(image.value?.url || ''); image.value = null
  inputAudio.value?.pause(); revoke(audioUrl.value); audioUrl.value = ''; audioFile.value = null
}
function pickModel() {
  const preferred: Partial<Record<ExperienceKind, string>> = { cv: 'yolov11_n', tts: 'matcha-zh' }
  if (!choices.value.some(item => item.id === model.value)) model.value = choices.value.find(item => item.id === preferred[kind.value] && item.status === 'online')?.id || choices.value.find(item => item.status === 'online')?.id || choices.value[0]?.id || ''
}
watch(kind, value => { clearMedia(); clearResult(); prompt.value = defaults[value]; stream.value = value === 'chat'; pickModel() })
watch(choices, pickModel)
watch(model, () => { if (!running.value) clearResult() })
watch([prompt, stream, maxTokens, temperature, confidence, iou, speed, language], () => { if (!running.value) clearResult() })

async function selectImage(event: Event) {
  if (running.value) return
  const file = (event.target as HTMLInputElement).files?.[0]; (event.target as HTMLInputElement).value = ''
  clearMedia(); clearResult(); if (!file) return
  const revision = ++selectionVersion; preparing.value = true
  try {
    const prepared = await prepareImage(file)
    if (disposed || revision !== selectionVersion) revoke(prepared.url)
    else image.value = prepared
  } catch (reason) { if (revision === selectionVersion) error.value = describeError(reason) }
  finally { if (revision === selectionVersion) preparing.value = false }
}
function selectAudio(event: Event) {
  if (running.value) return
  const file = (event.target as HTMLInputElement).files?.[0]; (event.target as HTMLInputElement).value = ''
  clearMedia(); clearResult(); if (!file) return
  if (!/\.wav$/i.test(file.name) || file.size > 20 * 1024 * 1024) { error.value = '请选择不超过 20 MB 的 PCM WAV 文件'; return }
  audioFile.value = file; audioUrl.value = URL.createObjectURL(file)
}
async function copyOutput() {
  try { await navigator.clipboard.writeText(output.value); copied.value = true }
  catch { notice.value = '浏览器未允许自动复制，请在结果文字中手动选择并复制。' }
}
async function seek(start: number) {
  const audio = inputAudio.value
  if (!audio || !Number.isFinite(audio.duration) || start >= audio.duration) { notice.value = '音频尚未就绪或时间戳超出音频范围'; return }
  audio.currentTime = start
  try { await audio.play() } catch { notice.value = '请点击音频播放器的播放按钮试听。' }
}
function stop() { notice.value = '已停止接收，已有内容可能不完整。模型端计算不保证立即停止，请勿立即反复提交。'; controller?.abort() }

async function run() {
  if (!canRun.value || !selected.value) return
  clearResult(); running.value = true; resultModel.value = model.value; resultKind.value = kind.value; sentConfidence.value = confidence.value
  requestId.value = newEvaluationId()
  const current = new AbortController(); controller = current
  const timeout = setTimeout(() => { notice.value = '请求达到 180 秒接收时限，结果待确认；未重试。'; current.abort() }, 180000)
  const started = performance.now()
  const headers: Record<string, string> = { 'X-CSRF-Token': getCsrf(), 'X-Request-ID': requestId.value }
  let endpoint: string, body: BodyInit
  if (isChat.value) {
    endpoint = '/api/test/chat'; headers['Content-Type'] = 'application/json'
    body = JSON.stringify({ model: model.value, prompt: prompt.value, stream: stream.value, max_tokens: maxTokens.value, temperature: temperature.value, image_url: needsImage.value ? image.value?.data : null })
  } else if (kind.value === 'cv') {
    endpoint = `/api/test/cv/${selected.value.capability.slice(3)}?${new URLSearchParams({ model: model.value, confidence: String(confidence.value), iou: String(iou.value) })}`
    headers['Content-Type'] = 'image/png'; body = image.value!.blob
  } else if (kind.value === 'asr') {
    endpoint = `/api/test/asr?${new URLSearchParams({ model: model.value, language: language.value })}`; headers['Content-Type'] = 'audio/wav'; body = audioFile.value!
  } else { endpoint = '/api/test/tts'; headers['Content-Type'] = 'application/json'; body = JSON.stringify({ model: model.value, input: prompt.value, speed: speed.value }) }
  try {
    const response = await fetch(endpoint, { method: 'POST', credentials: 'include', headers, body, signal: current.signal })
    if (disposed || controller !== current) return
    metrics.value.status = response.status; metrics.value.headers = performance.now() - started
    if (!response.ok) {
      const text = new TextDecoder().decode(await readResponse(response, 128 * 1024)); let detail: unknown = text
      try { detail = JSON.parse(text).detail || JSON.parse(text) } catch { /* plain error */ }
      throw new ApiError(response.status, detail)
    }
    if (isChat.value && stream.value) {
      await streamChat(response, text => { if (metrics.value.first === null) metrics.value.first = performance.now() - started; metrics.value.chunks++; output.value += text }, frame => { rawOutput.value += frame + '\n\n' })
    } else {
      const bytes = await readResponse(response)
      if (disposed || current.signal.aborted || controller !== current) return
      if (kind.value === 'tts') {
        if (!response.headers.get('Content-Type')?.startsWith('audio/wav') || new TextDecoder().decode(bytes.slice(0, 4)) !== 'RIFF' || new TextDecoder().decode(bytes.slice(8, 12)) !== 'WAVE') throw new Error('没有收到有效的 WAV 音频，未创建播放器')
        speechUrl.value = URL.createObjectURL(new Blob([bytes as BlobPart], { type: 'audio/wav' }))
        output.value = prompt.value; rawOutput.value = JSON.stringify({ content_type: 'audio/wav', bytes: bytes.length, model: resultModel.value, speed: speed.value }, null, 2)
      } else {
        rawOutput.value = new TextDecoder().decode(bytes); rawValue.value = JSON.parse(rawOutput.value)
        rawOutput.value = JSON.stringify(rawValue.value, null, 2)
        if (isChat.value) {
          const content = (rawValue.value as any)?.choices?.[0]?.message?.content
          if (typeof content !== 'string') throw new Error('接口未返回文本内容，请查看原始响应')
          output.value = content; metrics.value.chunks = content ? 1 : 0
        } else if (kind.value === 'asr') output.value = transcriptView(rawValue.value).text
      }
    }
    if (!current.signal.aborted && !disposed) done.value = true
  } catch (reason) { if (!disposed && (reason as Error).name !== 'AbortError') error.value = describeError(reason) }
  finally { clearTimeout(timeout); if (!disposed && controller === current) { metrics.value.total = performance.now() - started; running.value = false; controller = null } }
}

onMounted(async () => {
  try { await refreshOverview() } catch { /* overviewError shown */ }
  if (disposed) return
  const requested = typeof route.query.model === 'string' ? models.value.data.find(item => item.id === route.query.model) : null
  if (requested) { const target = experiences.find(item => matchesExperience(requested, item.id)); if (target) { kind.value = target.id; model.value = requested.id } }
  else pickModel()
})
onUnmounted(() => { disposed = true; controller?.abort(); controller = null; clearMedia(); revoke(speechUrl.value) })
</script>

<template>
  <section class="experience-page">
    <header class="experience-intro"><div><h2>看见、听见模型的真实输出</h2><p>选择能力，上传样本，直接对照结果。原始接口响应仍可展开查看。</p></div><button class="exp-button" :disabled="running" @click="refreshOverview().catch(() => undefined)"><RefreshCw :size="15" />刷新模型状态</button></header>
    <div v-if="overview?.host.platform?.notice" class="notice">{{ overview.host.platform.notice }}</div>
    <nav class="experience-tabs" aria-label="选择体验类型"><button v-for="item in availableExperiences" :key="item.id" :disabled="running" :aria-pressed="kind === item.id" :data-experience="item.id" @click="kind = item.id"><strong>{{ item.title }}</strong><small>{{ item.detail }}</small></button></nav>
    <p v-if="overviewError" class="exp-warning" role="alert">模型状态读取失败：{{ overviewError }}。刷新成功前不能发送请求。</p>
    <div class="experience-layout">
      <section class="panel experience-form"><header><h3>请求配置</h3><span>真实网关调用</span></header>
        <fieldset :disabled="running" class="exp-fields">
          <label>模型<select v-model="model" aria-label="模型"><option v-if="!choices.length" value="">此能力暂无模型</option><option v-for="item in choices" :key="item.id" :value="item.id" :disabled="item.status !== 'online'">{{ item.id }} · {{ item.status === 'online' ? '在线' : item.status === 'inactive' ? '未激活' : '不可用' }}</option></select></label>
          <label v-if="needsImage">{{ kind === 'cv' ? '检测图片' : kind === 'ocr' ? '识别图片' : '问答图片' }}<input type="file" accept="image/png,image/jpeg,image/webp" @change="selectImage" /><small>{{ preparing ? '正在准备图片…' : image ? `${image.name} · ${image.width} × ${image.height}` : 'PNG / JPEG / WebP，最多 12 MB' }}</small></label>
          <label v-if="kind === 'asr'">识别音频<input type="file" accept=".wav,audio/wav,audio/x-wav" @change="selectAudio" /><small>{{ audioFile?.name || 'PCM 16-bit WAV，单/双声道，8–48 kHz，最长 5 分钟、20 MB' }}</small></label>
          <label v-if="kind === 'asr'">识别语言<select v-model="language"><option value="auto">自动识别</option><option value="zh">中文</option><option value="en">英语</option><option value="ja">日语</option><option value="ko">韩语</option><option value="yue">粤语</option></select></label>
          <label v-if="isChat || kind === 'tts'">{{ kind === 'tts' ? '合成文字' : 'Prompt' }}<textarea v-model="prompt" :maxlength="kind === 'tts' ? 500 : 32000" rows="6"></textarea><small v-if="kind === 'tts'">{{ prompt.length }} / 500 字符 · 当前模型仅提供已安装的中文女声</small></label>
          <div v-if="kind === 'cv'" class="exp-two"><label>请求置信度<input v-model.number="confidence" type="number" min="0" max="1" step="0.05" /></label><label>NMS IoU<input v-model.number="iou" type="number" min="0" max="1" step="0.05" /></label></div>
          <label v-if="kind === 'tts'">语速<select v-model.number="speed"><option :value=".75">0.75×</option><option :value="1">1×</option><option :value="1.25">1.25×</option><option :value="1.5">1.5×</option></select></label>
          <details v-if="isChat" class="exp-advanced"><summary>生成参数</summary><div class="exp-two"><label>最大输出 Token<input v-model.number="maxTokens" type="number" min="1" max="2048" /></label><label>Temperature<input v-model.number="temperature" type="number" min="0" max="2" step="0.1" /></label></div><label class="exp-check"><input v-model="stream" type="checkbox" />流式输出 SSE</label></details>
        </fieldset>
        <p class="exp-scope">上传内容仅在点击发送后交给本机管理网关与模型处理，不保存到体验历史。真实推理会占用设备资源。</p>
        <button v-if="!running" class="exp-button primary" :disabled="!canRun" @click="run"><Play :size="16" />{{ kind === 'tts' ? '生成语音' : '发送请求' }}</button><button v-else class="exp-button danger" @click="stop"><Square :size="16" />停止接收</button>
        <p class="exp-scope">{{ kind === 'asr' ? '其他音频格式可先转为 WAV，长音频请使用批量任务。' : '不会自动重试、切模式或重启模型。' }}</p>
      </section>
      <div class="experience-results">
        <p v-if="error" role="alert" class="exp-error"><TriangleAlert :size="17" />{{ error }}</p><p v-if="notice" role="status" class="exp-warning">{{ notice }}</p>
        <section class="panel experience-output"><header><div><h3>{{ kind === 'cv' ? '检测结果' : kind === 'ocr' ? '原图与识别文字' : kind === 'asr' ? '原音频与转写' : kind === 'tts' ? '语音试听' : kind === 'vision' ? '图片与回答' : '模型响应' }}</h3><span v-if="running"><LoaderCircle class="spin" :size="14" />接收中</span><span v-else-if="done" class="exp-success">完成</span><span v-else>等待请求</span></div><button v-if="output && kind !== 'tts'" class="exp-button" @click="copyOutput"><Copy :size="14" />{{ copied ? '已复制' : '复制文字' }}</button></header>
          <div class="experience-result-body">
            <template v-if="kind === 'cv' && image && cv"><DetectionPreview :key="requestId" :source="image" :result="cv" :minimum="sentConfidence" /></template>
            <div v-else-if="needsImage" class="image-text-compare" :class="{ 'single-image': kind === 'cv' }"><figure v-if="image"><img :src="image.url" :alt="kind === 'ocr' ? '本次 OCR 原图' : '本次提交图片'" /><figcaption>{{ image.name }} · {{ image.width }} × {{ image.height }}<small>预览与提交使用相同像素，已按显示方向规范化。</small></figcaption></figure><div v-else class="experience-placeholder">先选择一张图片，在这里查看输入与结果。</div><section v-if="kind !== 'cv'" class="text-result"><h4>{{ kind === 'ocr' ? '识别文字' : '模型回答' }}</h4><pre :class="{ placeholder: !output }">{{ output || (done ? '模型返回了空文本。' : '结果会显示在这里，可与左侧图片对照。') }}</pre></section></div>
            <div v-else-if="kind === 'asr'" class="audio-compare"><section><h4>原音频</h4><template v-if="audioUrl"><p class="media-name">{{ audioFile?.name }}</p><audio ref="inputAudio" :src="audioUrl" controls preload="metadata" aria-label="原音频播放器" @timeupdate="activeTime = inputAudio?.currentTime || 0" @error="previewAudioError = '浏览器无法预览此音频，请检查 WAV 编码。'"></audio><p v-if="previewAudioError" class="exp-warning">{{ previewAudioError }}</p></template><p v-else class="experience-placeholder">选择 WAV 文件后即可试听原音频。</p></section><section class="text-result"><h4>转写文字</h4><pre>{{ output || (done ? '模型返回了空转写，可能没有识别到语音。' : '发送请求后，转写将显示在这里。') }}</pre><div v-if="transcript?.segments.length" class="transcript-segments"><p>点击时间段，定位试听原音频。</p><button v-for="(segment, index) in transcript.segments" :key="index" :class="{ current: activeTime >= segment.start && activeTime < segment.end }" @click="seek(segment.start)"><time>{{ seconds(segment.start) }}–{{ seconds(segment.end) }}</time><span>{{ segment.text }}</span></button></div><small v-else-if="done">本次响应没有有效分段时间戳，仅显示全文。</small></section></div>
            <div v-else-if="kind === 'tts'" class="speech-result"><h4>合成文字</h4><blockquote>{{ output || prompt }}</blockquote><template v-if="speechUrl"><h4>生成音频</h4><audio :src="speechUrl" controls preload="metadata" aria-label="合成音频播放器" @error="previewAudioError = '浏览器无法播放返回音频，请下载 WAV 后检查。'"></audio><p v-if="previewAudioError" class="exp-warning">{{ previewAudioError }}</p><a class="exp-button" :href="speechUrl" :download="`${resultModel}-${requestId}.wav`"><Download :size="14" />下载 WAV</a><small>点击播放试听，不会自动播放或再次调用模型。</small></template><p v-else class="experience-placeholder">生成完成后，可在这里直接试听。</p></div>
            <pre v-else class="chat-result">{{ output || (done ? '模型返回了空内容。' : '响应内容将在这里逐段显示…') }}</pre>
          </div>
        </section>
        <section class="panel experience-metrics"><div><small>HTTP</small><strong>{{ metrics.status ?? '—' }}</strong></div><div><small>响应头</small><strong>{{ metrics.headers === null ? '—' : `${metrics.headers.toFixed(0)} ms` }}</strong></div><div><small>总耗时</small><strong>{{ metrics.total === null ? '—' : `${(metrics.total / 1000).toFixed(2)} s` }}</strong></div><div v-if="isChat && stream"><small>首字 / 内容块</small><strong>{{ metrics.first === null ? '—' : `${metrics.first.toFixed(0)} ms` }} / {{ metrics.chunks }}</strong></div></section>
        <details class="panel experience-debug"><summary>原始响应与请求信息</summary><dl><dt>请求 ID</dt><dd>{{ requestId || '尚未发送' }}</dd><dt>本次模型</dt><dd>{{ resultModel || '—' }}</dd><template v-if="isChat"><dt>响应结束</dt><dd>{{ done ? stream ? '[DONE]' : 'JSON 完成' : '尚未完整结束' }}</dd></template></dl><pre>{{ rawOutput || '原始响应将在请求后显示。' }}</pre><small>耗时包含网关与网络等待，不是模型纯计算耗时。此页不保存输入或结果；刷新页面会清空。</small></details>
      </div>
    </div>
  </section>
</template>

<style scoped>
.experience-page { display:grid; gap:20px; min-width:0; }.experience-intro { display:flex; justify-content:space-between; gap:16px; align-items:center; }.experience-intro h2 { margin:0 0 8px; font-size:22px; }.experience-intro p { color:var(--muted); margin:0; font-size:13px; }.exp-button { display:inline-flex; justify-content:center; align-items:center; gap:7px; border:1px solid #cadbd2; background:white; color:#325e4e; padding:10px 14px; border-radius:8px; font-size:12px; cursor:pointer; text-decoration:none; }.exp-button.primary { background:#167750; color:white; border-color:#167750; }.exp-button.danger { color:#af3c35; border-color:#d5aaa6; }.exp-button:disabled,fieldset:disabled { opacity:.55; }.exp-button:disabled { cursor:not-allowed; }.experience-tabs { display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:8px; }.experience-tabs button { background:white; border:1px solid var(--line); padding:16px 12px; text-align:left; border-radius:9px; cursor:pointer; }.experience-tabs button[aria-pressed=true] { border-color:#167750; background:#eaf5ee; box-shadow:inset 0 0 0 1px #167750; }.experience-tabs strong { display:block; font-size:14px; }.experience-tabs small { display:block; color:#74847d; font-size:11px; margin-top:7px; line-height:1.5; }.experience-layout { display:grid; grid-template-columns:300px minmax(0,1fr); gap:20px; align-items:start; }.experience-form { padding:18px; }.experience-form header,.experience-output header { display:flex; justify-content:space-between; align-items:center; gap:12px; }.experience-form h3,.experience-output h3 { margin:0; font-size:15px; }.experience-form header span { font-size:10px; color:#74847d; }.exp-fields { border:0; padding:0; margin:20px 0; display:grid; gap:17px; min-width:0; }.exp-fields label { display:grid; gap:8px; font-size:12px; font-weight:600; min-width:0; }.exp-fields input:not([type=checkbox]),.exp-fields textarea,.exp-fields select { width:100%; min-width:0; border:1px solid #cfdcd5; border-radius:7px; padding:10px; font:inherit; color:var(--ink); background:white; }.exp-fields input[type=file] { font-size:11px; padding:8px; }.exp-fields textarea { resize:vertical; line-height:1.6; }.exp-fields small,.exp-scope { font-size:11px; line-height:1.65; color:#788a80; font-weight:400; overflow-wrap:anywhere; }.exp-two { display:grid; grid-template-columns:1fr 1fr; gap:10px; }.exp-advanced summary { font-size:12px; cursor:pointer; margin-bottom:14px; }.exp-check { display:flex!important; align-items:center; margin-top:14px; }.experience-form>.exp-button { width:100%; }.experience-results { display:grid; gap:16px; min-width:0; }.experience-output { overflow:hidden; min-width:0; }.experience-output header { padding:18px; border-bottom:1px solid var(--line); }.experience-output header>div { display:flex; align-items:center; gap:12px; }.experience-output header span { display:inline-flex; align-items:center; gap:4px; font-size:11px; color:#7b8c84; }.experience-output header .exp-success { color:#117a50; }.experience-result-body { padding:20px; min-height:320px; }.image-text-compare,.audio-compare { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1fr); gap:20px; align-items:start; }.image-text-compare.single-image { grid-template-columns:1fr; }figure { margin:0; min-width:0; }figure img { display:block; width:100%; max-height:560px; object-fit:contain; border:1px solid #dde6e0; background:#eef3f0; }figcaption { font-size:11px; color:#6b8073; line-height:1.5; padding:10px 0; overflow-wrap:anywhere; }figcaption small { display:block; margin-top:4px; }.text-result h4,.audio-compare h4,.speech-result h4 { margin:0 0 12px; font-size:13px; }.text-result pre,.chat-result { margin:0; white-space:pre-wrap; overflow-wrap:anywhere; font:14px/1.9 'Segoe UI','Microsoft YaHei',sans-serif; min-height:220px; max-height:600px; overflow:auto; background:#f5f8f6; padding:16px; border-radius:8px; }.text-result small,.speech-result small { display:block; font-size:11px; line-height:1.7; color:#798980; margin-top:12px; }.experience-placeholder { display:grid; place-content:center; min-height:200px; border:1px dashed #cfdcd5; border-radius:8px; padding:24px; color:#798d80; font-size:13px; line-height:1.8; }.audio-compare audio,.speech-result audio { display:block; width:100%; margin:14px 0 20px; }.media-name { overflow-wrap:anywhere; font-size:12px; color:#63786b; }.speech-result blockquote { margin:0 0 24px; padding:16px; background:#f2f7f4; border-left:3px solid #5c9980; font-size:14px; line-height:1.9; white-space:pre-wrap; overflow-wrap:anywhere; }.transcript-segments { margin-top:16px; max-height:280px; overflow:auto; }.transcript-segments p { font-size:12px; color:#687b70; }.transcript-segments button { display:grid; grid-template-columns:100px minmax(0,1fr); text-align:left; gap:10px; width:100%; background:white; border:1px solid #dce6e0; padding:10px; margin-bottom:7px; border-radius:6px; font-size:12px; }.transcript-segments button.current { background:#e7f4ec; border-color:#57a581; }.transcript-segments time { color:#167750; font-size:11px; }.transcript-segments span { overflow-wrap:anywhere; }.experience-metrics { display:flex; gap:22px; flex-wrap:wrap; padding:17px 20px; }.experience-metrics small,.experience-metrics strong { display:block; }.experience-metrics small { color:#768a7e; font-size:11px; }.experience-metrics strong { font-size:14px; margin-top:7px; }.experience-debug { padding:17px 20px; overflow:hidden; }.experience-debug summary { cursor:pointer; font-size:12px; color:#4d6d5c; }.experience-debug dl { display:grid; grid-template-columns:90px minmax(0,1fr); gap:10px; font-size:12px; }.experience-debug dd { margin:0; overflow-wrap:anywhere; }.experience-debug pre { padding:15px; border-radius:6px; background:#122c30; color:#d2e5db; white-space:pre-wrap; overflow-wrap:anywhere; max-height:360px; overflow:auto; font-size:11px; }.experience-debug small { color:#7c8f81; font-size:11px; }.exp-warning,.exp-error { display:flex; gap:8px; align-items:start; padding:12px 15px; border-radius:8px; font-size:12px; line-height:1.7; margin:0; overflow-wrap:anywhere; }.exp-warning { background:#fff5e4; color:#8b5a15; }.exp-error { background:#ffefeb; color:#9e3529; }
@media(max-width:1200px) { .experience-tabs { grid-template-columns:repeat(3,minmax(0,1fr)); }.experience-layout { grid-template-columns:260px minmax(0,1fr); }.image-text-compare,.audio-compare { grid-template-columns:1fr; } }
@media(max-width:800px) { .experience-layout { grid-template-columns:1fr; }.experience-intro { align-items:start; flex-direction:column; }.experience-intro h2 { font-size:19px; }.experience-result-body { padding:14px; }.experience-tabs { grid-template-columns:repeat(2,minmax(0,1fr)); }.experience-output header>div { flex-wrap:wrap; } }
</style>
