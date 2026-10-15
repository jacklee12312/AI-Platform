<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter, RouterLink } from 'vue-router'
import { BookOpen, Check, Copy, Download, RefreshCw, Search, X } from '@lucide/vue'
import { api, describeError } from '../api'

interface Evidence { title: string; date: string; path: string; sha256: string; summary: string }
interface Example { id: string; title: string; method: string; path: string; transport: string; note: string; snippets: Record<string, string> }
interface Capability {
  id: string; name: string; category: string; profile: string; implementation: string; summary: string;
  registered: boolean | null; runtime_status: string; active_mode: string | null; modes: string[];
  baseline_match: boolean; differences: string[]; unreported_versions: string[]; evidence_applicability: string;
  declared_context?: number; declared_slots?: number;
  baseline?: { metadata: { model_version?: string; service_version?: string } } | null;
  inputs: string[]; outputs: string[]; streaming: string[]; boundaries: string[];
  verification: { status: string; scope: string; sources: string[] }; examples: Example[];
}
interface Catalog { reviewed_at: string; checked_at: string; registry_available: boolean; config_available: boolean; notice: string; config_notice: string; base_url: string;
  limits: { id: string; label: string; value: number; unit: string; source: string }[]; sources: Record<string, Evidence>; models: Capability[] }
const route = useRoute(), router = useRouter()
const catalog = ref<Catalog | null>(null), loading = ref(false), error = ref(''), query = ref(''), filter = ref('all')
const selectedId = ref(''), exampleId = ref(''), language = ref('python'), copied = ref(false), copyMessage = ref('')
const codeArea = ref<HTMLTextAreaElement | null>(null)
const dialog = ref<HTMLElement | null>(null)
let previousFocus: HTMLElement | null = null
let previousOverflow = ''
function trapFocus(event: KeyboardEvent) {
  const targets = Array.from(dialog.value?.querySelectorAll<HTMLElement>('button, a, select, textarea, summary') || [])
  const first = targets[0], last = targets[targets.length - 1]
  if (event.shiftKey && [first, dialog.value].includes(document.activeElement as HTMLElement)) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
watch(selectedId, async (value, previous) => {
  if (value && !previous) {
    previousFocus = document.activeElement as HTMLElement; previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'; await nextTick(); dialog.value?.focus()
  } else if (!value && previous) { document.body.style.overflow = previousOverflow; previousFocus?.focus() }
})
onUnmounted(() => { if (selectedId.value) document.body.style.overflow = previousOverflow })
const implementation: Record<string, string> = { implemented: '有接口实现', partial: '部分实现', unavailable: '未交付', unknown: '说明待补齐' }
const verification: Record<string, string> = { sample_verified: '指定样例通过', historical: '历史验证记录', issues: '已知问题待闭环', pending: '待专项验收', blocked: '适配受阻' }
const runtime: Record<string, string> = { online: '健康探测在线', inactive: '当前模式未激活', unavailable: '不可用 / 已停用', error: '健康探测异常', unknown: '运行状态未知', not_registered: '未注册到网关' }
const modes: Record<string, string> = {
  copaw: 'CoPaw 独占模式', 'spark-x1': 'Spark X1 独占模式', qwen38: 'Qwen3.8 独占模式', qwen36: 'Qwen3.6 双卡独占模式',
  'qwen-vl': 'Qwen3-VL 独占模式', 'glm-ocr': 'GLM-OCR 独占模式',
  'bge-m3': 'BGE-M3 独占模式', reranker: 'BGE Reranker 独占模式', cosyvoice: 'CosyVoice3 独占模式',
}
const languages: Record<string, string> = { python: 'Python', bash: 'curl · Bash', powershell: 'PowerShell · curl.exe' }
const filtered = computed(() => (catalog.value?.models || []).filter(item => {
  const matches = `${item.id} ${item.name} ${item.category} ${item.summary}`.toLowerCase().includes(query.value.toLowerCase())
  return matches && (filter.value === 'all' || filter.value === 'issues' && ['issues', 'blocked'].includes(item.verification.status)
    || filter.value === 'partial' && item.implementation === 'partial' || filter.value === 'verified' && item.verification.status === 'sample_verified'
    || filter.value === 'pending' && ['pending', 'historical'].includes(item.verification.status))
}))
const selected = computed(() => catalog.value?.models.find(item => item.id === selectedId.value))
const example = computed(() => selected.value?.examples.find(item => item.id === exampleId.value) || selected.value?.examples[0])
const code = computed(() => example.value?.snippets[language.value] || '')
const registeredCount = computed(() => catalog.value?.models.filter(item => item.registered === true).length || 0)
const partialCount = computed(() => catalog.value?.models.filter(item => item.implementation === 'partial').length || 0)
const issuesCount = computed(() => catalog.value?.models.filter(item => ['issues', 'blocked'].includes(item.verification.status)).length || 0)
function pick(id: string) { selectedId.value = id; router.replace({ query: { ...route.query, model: id } }) }
function close() { selectedId.value = ''; const { model: _model, ...rest } = route.query; router.replace({ query: rest }) }
async function load() {
  if (loading.value) return
  loading.value = true; error.value = ''
  try {
    catalog.value = await api<Catalog>('/api/model-capabilities')
    if (typeof route.query.model === 'string') selectedId.value = route.query.model
  } catch (err) { error.value = describeError(err); catalog.value = null }
  finally { loading.value = false }
}
watch(() => route.query.model, value => { selectedId.value = typeof value === 'string' ? value : '' })
watch(selectedId, () => { exampleId.value = selected.value?.examples[0]?.id || ''; language.value = 'python'; copied.value = false; copyMessage.value = '' })
watch(selected, value => { if (!value?.examples.some(item => item.id === exampleId.value)) exampleId.value = value?.examples[0]?.id || '' })
watch(example, value => { if (value && !value.snippets[language.value]) language.value = 'python' })
watch(code, () => { copied.value = false; copyMessage.value = '' })
async function copyCode() {
  copied.value = false; copyMessage.value = ''
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(code.value)
    else {
      codeArea.value?.focus(); codeArea.value?.select()
      if (!document.execCommand('copy')) throw new Error('clipboard unavailable')
    }
    copied.value = true; copyMessage.value = '请求示例已复制，执行前请设置自己的 API_KEY 并准备输入文件。'
  } catch { codeArea.value?.focus(); codeArea.value?.select(); copyMessage.value = '浏览器未允许自动复制，代码已选中，请按 Ctrl+C（macOS 为 ⌘C）。' }
}
onMounted(load)
</script>

<template>
  <div class="cap-page">
    <div class="cap-intro"><div><h2>看清能力，再开始接入</h2><p>模式、输入边界、返回协议与验收证据，一处查明。</p></div><a class="cap-button" href="/api/model-capabilities/export.md"><Download :size="15" />下载接入说明</a></div>
    <div class="cap-notice"><BookOpen :size="19" /><div><strong>注册 ≠ 推理可用 ≠ 质量达标 ≠ 正式交付</strong><p>{{ catalog?.notice || '查看模型的具体实现范围和验收记录，避免把模型名称当成完整业务能力。' }}</p></div></div>
    <p v-if="error" class="cap-error" role="alert">{{ error }}</p>
    <p v-if="catalog && !catalog.registry_available" class="cap-error" role="alert">网关状态读取失败。以下保留静态能力说明，运行状态标为未知，暂不提供可执行示例。</p>
    <div class="cap-metrics"><div><strong>{{ catalog?.registry_available ? registeredCount : '—' }}</strong><span>当前网关注册</span></div><div><strong>{{ partialCount }}</strong><span>部分实现，明确边界</span></div><div><strong>{{ issuesCount }}</strong><span>已知问题 / 适配受阻</span></div><div><strong>{{ catalog?.reviewed_at || '—' }}</strong><span>说明核对日期</span></div></div>
    <div class="panel cap-toolbar"><label class="cap-search"><Search :size="16" /><input v-model="query" aria-label="搜索模型能力" placeholder="搜索模型、能力或用途" /></label><select v-model="filter" aria-label="按能力证据筛选"><option value="all">全部模型</option><option value="verified">指定样例通过</option><option value="partial">部分实现</option><option value="issues">已知问题 / 受阻</option><option value="pending">历史记录 / 待验收</option></select><button class="cap-button" :disabled="loading" @click="load"><RefreshCw :size="14" :class="{ spin: loading }" />{{ loading ? '读取中…' : '刷新状态' }}</button></div>
    <div v-if="loading && !catalog" class="cap-empty">正在核对模型状态与配置…</div>
    <div class="cap-grid">
      <button v-for="item in filtered" :key="item.id" class="panel cap-card" :class="{ selected: selectedId === item.id }" :aria-label="`查看 ${item.id} 能力说明`" @click="pick(item.id)">
        <div class="cap-card-top"><span>{{ item.category }}</span><span class="cap-chip" :data-state="item.implementation">{{ implementation[item.implementation] }}</span></div>
        <h3>{{ item.name }}</h3><code>{{ item.id }}</code><p>{{ item.summary }}</p>
        <div class="cap-card-bottom"><span class="cap-chip" :data-state="item.verification.status">{{ verification[item.verification.status] }}</span><span class="cap-runtime" :data-state="item.runtime_status">{{ runtime[item.runtime_status] || item.runtime_status }}</span></div>
        <small>{{ item.modes.length ? item.modes.map(mode => modes[mode] || mode).join(' / ') : item.implementation === 'unavailable' ? '尚无可用业务模式' : '注册表无模式限制，仍需服务健康' }}</small>
        <small v-if="item.differences.length" class="cap-warning">版本或协议登记发生变化，历史证据需重新核对</small>
        <small v-else-if="item.unreported_versions.length">版本未上报 · 验收适用性未确认</small>
      </button>
    </div>
    <p v-if="catalog && !filtered.length" class="cap-empty">没有匹配的模型说明</p>
    <details v-if="catalog" class="panel cap-limits"><summary>请求限制与配置来源</summary><p>{{ catalog.config_notice }}</p><div class="cap-limit-grid"><div v-for="limit in catalog.limits" :key="limit.id"><span>{{ limit.label }}</span><strong>{{ limit.value }} {{ limit.unit }}</strong><small>{{ limit.source === 'saved_config' ? '当前保存配置' : '源码默认 · 未确认运行值' }}</small></div></div><p>请求还受 Key 权限、频率、每日额度及共享资源并发限制。返回 429 时按错误码处理；模式未激活通常返回 503 model_inactive。输入超过模型上下文的行为需单独验证。</p><RouterLink to="/usage">查看 Key 与资源控制 →</RouterLink></details>

    <div v-if="selectedId" class="cap-overlay" @click.self="close" @keydown.esc="close">
      <section ref="dialog" class="cap-dialog" role="dialog" aria-modal="true" :aria-label="`${selected?.id || selectedId} 能力说明`" tabindex="-1" @keydown.tab="trapFocus">
        <header><div><small>{{ selected?.category || '模型说明' }}</small><h2>{{ selected?.name || selectedId }}</h2><code>{{ selectedId }}</code></div><button class="cap-button" aria-label="关闭能力说明" @click="close"><X :size="18" /></button></header>
        <div v-if="!selected" class="cap-dialog-body"><p>{{ loading ? '正在读取…' : error || '尚未找到该模型说明，请刷新注册表后重试。' }}</p></div>
        <div v-else class="cap-dialog-body">
          <p class="cap-summary">{{ selected.summary }}</p>
          <div class="cap-state-row"><span class="cap-chip" :data-state="selected.implementation">{{ implementation[selected.implementation] }}</span><span class="cap-chip" :data-state="selected.verification.status">{{ verification[selected.verification.status] }}</span><span>{{ runtime[selected.runtime_status] || selected.runtime_status }}</span></div>
          <div class="cap-mode"><strong>支持模式</strong><p>{{ selected.modes.length ? selected.modes.map(mode => `${modes[mode] || mode}（${mode}）`).join(' / ') : selected.implementation === 'unavailable' ? '未提供可用业务模式' : '注册表无模式限制；独立服务仍需启动并通过健康探测。' }}</p><small v-if="selected.active_mode">当前模式：{{ selected.active_mode }}</small></div>
          <p v-if="selected.runtime_status !== 'online'" class="cap-warning">当前{{ runtime[selected.runtime_status] || selected.runtime_status }}。示例仅用于接入准备，执行前应确认模式、模型服务和 Key 授权。</p>
          <p v-if="selected.differences.length" class="cap-warning">注册元数据与说明基线不一致（{{ selected.differences.join('、') }}），已暂停示例。需更新说明并重新核查验收证据。</p>
          <p v-else-if="selected.unreported_versions.length" class="cap-warning">网关未上报版本（{{ selected.unreported_versions.join('、') }}）。协议登记匹配，可参考示例接入；历史验收是否适用于当前制品尚未确认。</p>
          <div class="cap-detail-grid"><section><h3>输入与限制</h3><ul><li v-for="value in selected.inputs" :key="value">{{ value }}</li><li v-if="!selected.inputs.length">尚未形成可用输入协议。</li></ul></section><section><h3>实际输出</h3><ul><li v-for="value in selected.outputs" :key="value">{{ value }}</li><li v-if="!selected.outputs.length">尚无已实现的业务输出。</li></ul><h3>流式形式</h3><ul><li v-for="value in selected.streaming" :key="value">{{ value }}</li></ul></section></div>
          <section class="cap-boundaries"><h3>接入前需要知道的边界</h3><ul><li v-for="value in selected.boundaries" :key="value">{{ value }}</li></ul></section>
          <section class="cap-evidence"><h3>验收覆盖与依据</h3><p>{{ selected.verification.scope }}</p><p class="cap-meta">{{ selected.baseline_match ? '注册元数据与说明基线相符' : '当前版本适用性尚未确认' }}；此比对不替代模型制品、驱动与镜像校验，也不自动签认质量。</p><p v-if="selected.baseline" class="cap-meta">说明基线：模型 {{ selected.baseline.metadata.model_version || '未记录' }} · 服务 {{ selected.baseline.metadata.service_version || '未记录' }}</p><details v-for="id in selected.verification.sources" :key="id"><summary>{{ catalog?.sources[id]?.title }} · {{ catalog?.sources[id]?.date }}</summary><p>{{ catalog?.sources[id]?.summary }}</p><small>来源：{{ catalog?.sources[id]?.path }}</small><code>SHA-256 {{ catalog?.sources[id]?.sha256 }}</code></details></section>
          <section v-if="selected.examples.length" class="cap-examples"><div class="cap-example-head"><h3>可复制调用示例</h3><span>公共网关 · 客户 API Key</span></div><p>当前访问地址对应的网关为 <code>{{ catalog?.base_url }}</code>，也可设置 GATEWAY_URL。准备自己的 API_KEY；示例不会读取或展示管理令牌。图片文件名为 image.png，ASR 音频为 input.wav，CosyVoice 参考音频为 reference.wav；保存 JSON 的 curl 示例会写入当前目录 request.json。</p><div class="cap-example-controls"><label>调用方式<select v-model="exampleId" aria-label="示例调用方式"><option v-for="item in selected.examples" :key="item.id" :value="item.id">{{ item.title }}</option></select></label><label>示例语言<select v-model="language" aria-label="示例语言"><option v-for="(_, key) in example?.snippets" :key="key" :value="key">{{ languages[key] || key }}</option></select></label><button class="cap-button" @click="copyCode"><Check v-if="copied" :size="15" /><Copy v-else :size="15" />{{ copied ? '已复制' : '复制请求' }}</button></div><p v-if="example?.note" class="cap-example-note">{{ example.note }}</p><code class="cap-endpoint">{{ example?.method }} {{ example?.path }}</code><textarea ref="codeArea" :value="code" readonly spellcheck="false" aria-label="可复制请求代码" class="cap-code"></textarea><p v-if="copyMessage" role="status" class="cap-copy-message">{{ copyMessage }}</p></section>
          <p v-else class="cap-warning">{{ selected.implementation === 'unavailable' ? '当前没有可用接口，不提供看似可执行的请求示例。' : '协议、版本或注册状态尚未确认，补齐说明前不生成推理示例。' }}</p>
          <div class="cap-related"><RouterLink v-if="['asr', 'ocr'].includes(selected.profile)" to="/batches">前往 OCR / ASR 批量任务 →</RouterLink><RouterLink to="/models">查看模型运行状态 →</RouterLink><RouterLink to="/usage">检查 Key 授权与配额 →</RouterLink></div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.cap-page{display:grid;gap:18px}.cap-intro{display:flex;justify-content:space-between;align-items:center;gap:16px}.cap-intro h2{font-size:22px}.cap-intro p{font-size:12px;color:var(--muted);margin-top:7px}.cap-button{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:8px 11px;border:1px solid var(--line);border-radius:7px;background:white;color:#315d50;font-size:12px;text-decoration:none;white-space:nowrap}.cap-notice{display:flex;align-items:flex-start;gap:12px;padding:17px 19px;background:#eaf6f0;border:1px solid #c4dfd2;border-radius:10px;color:#285746}.cap-notice strong{font-size:14px}.cap-notice p{font-size:12px;line-height:1.7;margin-top:5px}.cap-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.cap-metrics>div{padding:16px 19px;background:white;border:1px solid var(--line);border-radius:9px}.cap-metrics strong{display:block;font-size:22px}.cap-metrics span{display:block;margin-top:5px;font-size:11px;color:var(--muted)}.cap-toolbar{display:flex;gap:12px;align-items:center;padding:13px 17px}.cap-search{flex:1;display:flex;align-items:center;gap:8px}.cap-search input{width:100%;border:0;outline:none;min-width:0;font-size:13px}.cap-toolbar select,.cap-example-controls select{border:1px solid var(--line);border-radius:6px;background:white;padding:8px;font-size:12px;color:var(--ink)}.cap-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}.cap-card{padding:18px;text-align:left;color:var(--ink);transition:border-color .15s}.cap-card:hover,.cap-card.selected{border-color:#559b84}.cap-card:focus-visible,.cap-button:focus-visible{outline:2px solid #329b76;outline-offset:3px}.cap-card-top,.cap-card-bottom{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}.cap-card-top>span:first-child{font-size:11px;color:var(--muted)}.cap-card h3{font-size:15px;margin:14px 0 5px}.cap-card>code{font-size:11px;color:#7a8d88;overflow-wrap:anywhere}.cap-card>p{font-size:12px;line-height:1.8;margin:12px 0;min-height:44px}.cap-card>small{display:block;font-size:10px;color:var(--muted);line-height:1.6;margin-top:12px}.cap-chip{display:inline-block;font-size:10px;padding:4px 7px;border-radius:5px;background:#edf1f4;color:#576e79;white-space:nowrap}.cap-chip[data-state=sample_verified]{background:#e1f3e8;color:#26714d}.cap-chip[data-state=partial],.cap-chip[data-state=issues],.cap-chip[data-state=pending]{background:#fff0d8;color:#936526}.cap-chip[data-state=blocked],.cap-chip[data-state=unavailable]{background:#f6e5e2;color:#a1554c}.cap-runtime{font-size:10px;color:var(--muted)}.cap-runtime[data-state=online]{color:#2e8461}.cap-empty{padding:30px;text-align:center;color:var(--muted);font-size:13px}.cap-error{padding:12px;background:#fff0ea;color:#a04838;border-radius:8px;font-size:13px}.cap-limits{padding:18px}.cap-limits summary{cursor:pointer;font-size:14px;font-weight:600}.cap-limits p{font-size:12px;line-height:1.8;margin:12px 0;color:var(--muted)}.cap-limit-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.cap-limit-grid>div{padding:12px;background:#f5f8f6;border-radius:6px}.cap-limit-grid span,.cap-limit-grid small{display:block;font-size:11px;color:var(--muted)}.cap-limit-grid strong{display:block;margin:7px 0;font-size:16px}.cap-limits a,.cap-related a{font-size:12px;color:#2c765a;text-decoration:none}.cap-overlay{position:fixed;inset:0;z-index:90;background:rgba(13,31,31,.4);padding:22px;display:grid;place-items:center}.cap-dialog{background:white;width:min(1060px,100%);max-height:calc(100dvh - 44px);border-radius:13px;overflow:auto;box-shadow:0 20px 80px #102c303b}.cap-dialog>header{display:flex;justify-content:space-between;align-items:center;gap:15px;padding:20px 25px;border-bottom:1px solid var(--line);background:white;position:sticky;top:0;z-index:1}.cap-dialog header small{font-size:11px;color:var(--muted)}.cap-dialog header h2{font-size:21px;margin:5px 0}.cap-dialog header code{font-size:11px;color:var(--muted);overflow-wrap:anywhere}.cap-dialog-body{padding:22px 25px;display:grid;gap:18px}.cap-summary{font-size:14px;line-height:1.8}.cap-state-row{display:flex;gap:9px;align-items:center;flex-wrap:wrap}.cap-state-row>span:last-child{font-size:12px;color:var(--muted)}.cap-mode{padding:14px 16px;border:1px solid var(--line);border-radius:8px;font-size:12px}.cap-mode strong{font-size:13px}.cap-mode p{margin:7px 0;line-height:1.7}.cap-mode small{color:var(--muted)}.cap-detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:25px}.cap-dialog h3{font-size:14px;margin:0 0 10px}.cap-dialog ul{padding-left:18px;margin:0}.cap-dialog li{font-size:12px;line-height:1.9;margin-bottom:6px;color:#53665d}.cap-detail-grid h3:not(:first-child){margin-top:18px}.cap-boundaries{background:#fffaf1;border:1px solid #eadfc5;border-radius:8px;padding:16px}.cap-warning{font-size:12px;line-height:1.8;color:#94651b;padding:10px 13px;background:#fff4df;border-radius:6px}.cap-evidence>p{font-size:12px;line-height:1.8;margin:8px 0}.cap-evidence .cap-meta{color:var(--muted);font-size:11px}.cap-evidence details{border-bottom:1px solid #e7eeea;padding:11px 0}.cap-evidence summary{font-size:12px;cursor:pointer}.cap-evidence details p{margin:8px 0;font-size:12px;line-height:1.8;color:#677970}.cap-evidence details small,.cap-evidence details code{display:block;color:var(--muted);font-size:10px;line-height:1.9;overflow-wrap:anywhere}.cap-examples{border-top:1px solid var(--line);padding-top:18px;min-width:0}.cap-example-head{display:flex;justify-content:space-between;gap:10px}.cap-example-head span{font-size:11px;color:var(--muted)}.cap-examples>p{font-size:12px;line-height:1.8;color:#6a7b73;margin:8px 0}.cap-example-controls{display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;margin:14px 0}.cap-example-controls label{display:grid;gap:6px;font-size:11px;color:#75877d}.cap-endpoint{display:block;padding:10px 12px;background:#eaf3ee;color:#37654d;font-size:11px;overflow-wrap:anywhere;border-radius:6px 6px 0 0}.cap-code{display:block;width:100%;height:340px;resize:vertical;border:0;background:#12282d;color:#d6ede4;border-radius:0 0 7px 7px;padding:16px;font:12px/1.8 Consolas,monospace;white-space:pre;overflow:auto}.cap-examples .cap-copy-message{color:#28724e}.cap-related{display:flex;gap:18px;flex-wrap:wrap;border-top:1px solid var(--line);padding-top:16px}@media(max-width:1100px){.cap-grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:700px){.cap-intro,.cap-toolbar{flex-wrap:wrap}.cap-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.cap-metrics strong{font-size:19px}.cap-grid{grid-template-columns:1fr}.cap-toolbar .cap-search{flex-basis:100%}.cap-overlay{padding:8px}.cap-dialog{max-height:calc(100dvh - 16px)}.cap-dialog>header{padding:15px}.cap-dialog header h2{font-size:18px}.cap-dialog-body{padding:16px}.cap-detail-grid,.cap-limit-grid{grid-template-columns:1fr}.cap-example-head{flex-wrap:wrap}.cap-code{height:300px}.cap-metrics>div{padding:13px}}
</style>
