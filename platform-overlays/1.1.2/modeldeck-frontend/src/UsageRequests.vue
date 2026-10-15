<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { Clipboard, X } from '@lucide/vue'
import { requestStatus, tokenText, usageQuality } from './usagePresentation'
import { api, describeError } from './api'

export interface RequestDiagnostics {
  code: string
  reason: string
  suggestion: string
  not_inferred: boolean
  source?: string
  stage?: string
  http_status?: number
  upstream_http_status?: number
  upstream_dispatched?: boolean
  elapsed_ms?: number
  first_output_ms?: number
  last_output_ms?: number
  output_chunks?: number
  limit?: number
  in_flight?: number
  scope?: string
  retry_after?: number
  started_at?: string
  finished_at?: string
  finish_reason?: string
}

export interface UsageEvent {
  cancel_supported?: boolean
  cancel_requested?: boolean
  id: string
  request_id: string
  created_at: string
  endpoint: string
  model: string
  input_tokens: number | null
  output_tokens: number | null
  total_tokens: number | null
  usage_reported: boolean
  usage_quality?: string
  usage_source?: string
  usage_raw?: unknown
  usage_issues?: string[]
  status: string
  api_key_id: string
  key_name: string
  prefix: string
  diagnostics?: RequestDiagnostics
  request_summary?: string
  phase?: string
  request_metadata?: { client_ip?: string; client_port?: number; user_agent?: string; application?: string; method?: string; content_type?: string; stream?: boolean; message_count?: number; tool_count?: number; raw_bytes?: number; effective_parameters?: Record<string, unknown> }
  request_content?: {
    available: boolean
    payload: unknown
    reason?: string
    raw_bytes?: number | null
    truncated?: boolean
    redacted_fields?: number
    binary_fields?: number
  }
}

const props = withDefaults(defineProps<{ events: UsageEvent[]; emptyText?: string }>(), { emptyText:'当前范围暂无调用明细' })
const emit = defineEmits<{ cancelled: [] }>()
const cancelling = ref(new Set<string>()), cancelMessage = ref(''), cancelError = ref('')
const selected = ref<UsageEvent | null>(null)
const copyMessage = ref('')
const detailLoading = ref(false), detailError = ref('')
const dialog = ref<HTMLElement | null>(null)
let previousFocus: HTMLElement | null = null

watch(selected, async (value, old) => {
  if (value && value.id !== old?.id) {
    previousFocus = document.activeElement as HTMLElement
    await nextTick()
    dialog.value?.focus()
  } else if (!value) {
    previousFocus?.focus()
  } else {
    // A completed/cancelled request can remove the focused action button.
    await nextTick()
    if (dialog.value && !dialog.value.contains(document.activeElement)) dialog.value.focus()
  }
})

function dialogKey(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    selected.value = null
    return
  }
  if (event.key !== 'Tab') return
  const elements = Array.from(
    dialog.value?.querySelectorAll<HTMLElement>('button, summary, [tabindex="0"]') || [],
  ).filter(element => element.getClientRects().length)
  const first = elements[0]
  const last = elements[elements.length - 1]
  if (!first || !last) {
    event.preventDefault()
    return
  }
  if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog.value)) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}

onBeforeUnmount(() => { selected.value = null })

function diagnostic(item: UsageEvent): RequestDiagnostics {
  if (item.status === 'in_flight') return { ...item.diagnostics, code:'', reason:item.phase === 'finishing' ? '等待上游释放名额' : item.cancel_requested ? '正在中断，等待连接关闭' : stage(item.diagnostics?.stage), suggestion:'', not_inferred:false }
  return item.diagnostics || {
    code: '',
    reason: item.status === 'succeeded' ? '请求成功' : '历史原因未记录',
    suggestion: '',
    not_inferred: false,
  }
}

function date(value?: string) {
  return value
    ? new Intl.DateTimeFormat('zh-CN', {
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
      }).format(new Date(value))
    : '未记录'
}

function duration(value?: number) {
  return value == null ? '未记录' : value < 1000 ? `${value.toFixed(0)} ms` : `${(value / 1000).toFixed(2)} s`
}

function delivery(item: UsageEvent) {
  if (item.status === 'in_flight') return item.phase === 'finishing' ? '调用处理已结束，上游工作仍未结束，并发名额仍被占用。' : '正在占用一个网关并发名额；实际引擎调度状态请以引擎指标为准。'
  const details = diagnostic(item)
  if (details.code === 'admin_cancelled') return '管理员主动中断了本次请求；应用自动重试可能产生新的请求。'
  if (item.status === 'cancelled') return '调用连接已取消，具体原因未确认。'
  if (details.not_inferred) return '请求在网关被拒绝，未进入模型推理。'
  if (details.upstream_dispatched === true) return '已转交模型上游；不代表生成已完成。'
  return '是否进入模型推理未确认。'
}

function stage(value?: string) {
  return ({
    gateway: '网关请求检查',
    admission: '网关准入与资源控制',
    upstream: '等待模型响应',
    stream: '流式输出',
  } as Record<string, string>)[value || ''] || '未记录'
}

function concurrency(details: RequestDiagnostics) {
  const current = details.in_flight == null ? '未记录' : String(details.in_flight)
  const limit = details.limit == null ? '未记录' : details.limit === 0 ? '不限' : String(details.limit)
  return `${current} / ${limit}`
}

async function loadDetail(id: string) {
  detailLoading.value = true; detailError.value = ''
  try {
    const value = await api<UsageEvent>(`/api/requests/${encodeURIComponent(id)}`)
    if (selected.value?.id === id && value.id === id) selected.value = value
  } catch (error) { if (selected.value?.id === id) detailError.value = describeError(error) }
  finally { if (selected.value?.id === id) detailLoading.value = false }
}
function open(item: UsageEvent) {
  selected.value = item
  copyMessage.value = ''
  void loadDetail(item.id)
}
watch(() => props.events, values => {
  const current = selected.value
  if (!current || current.status !== 'in_flight') return
  const next = values.find(x => x.id === current.id)
  if (next) selected.value = { ...current, ...next }
  else { selected.value = { ...current, status:'ended_unrecorded' }; void loadDetail(current.id) }
})

async function copy(value: string) {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value)
    } else {
      const input = document.createElement('textarea')
      input.value = value
      input.style.position = 'fixed'
      input.style.opacity = '0'
      document.body.appendChild(input)
      try {
        input.select()
        if (!document.execCommand('copy')) throw new Error('copy failed')
      } finally {
        input.remove()
      }
    }
    copyMessage.value = '已复制 Request ID'
  } catch {
    copyMessage.value = '复制失败，请在详情中手动选择 Request ID'
  }
}

function requestContent(item: UsageEvent) {
  return item.request_content || {
    available: false,
    payload: null,
    reason: '历史记录未保存请求内容',
  }
}

function requestContentText(item: UsageEvent) {
  const content = requestContent(item)
  return content.available ? JSON.stringify(content.payload, null, 2) : content.reason || '请求内容未记录'
}

function requestSummary(item: UsageEvent) {
  if (item.request_summary) return item.request_summary
  const content = requestContent(item)
  if (!content.available) return content.reason || '请求内容未记录'
  const payload = content.payload as Record<string, unknown> | null
  let value: unknown = payload
  if (payload && typeof payload === 'object') {
    const messages = Array.isArray(payload.messages) ? payload.messages as Array<Record<string, unknown>> : []
    const user = [...messages].reverse().find(message => message.role === 'user')
    value = user?.content ?? payload.input ?? payload.query ?? payload
  }
  const text = (typeof value === 'string' ? value : JSON.stringify(value) ?? '').replace(/\s+/g, ' ').trim()
  return text.length > 64 ? text.slice(0, 64) + '…' : text || '已记录空请求体'
}

async function copyContent(item: UsageEvent) {
  await copy(requestContentText(item))
  if (copyMessage.value.startsWith('已复制')) copyMessage.value = '已复制请求内容'
}

async function cancelRequest(item: UsageEvent) {
  if (!item.cancel_supported || item.cancel_requested || cancelling.value.has(item.id)) return
  if (!window.confirm(`中断 ${item.key_name} 的 ${item.model} 请求？\nRequest ID：${item.request_id}\n\n只中断这条请求，应用将收到中断错误。应用自动重试可能重新占用名额；不会停止整个后台任务队列。`)) return
  cancelling.value.add(item.id); cancelError.value = ''; cancelMessage.value = ''
  try {
    const result = await api<{status:string;message:string}>(`/api/requests/${encodeURIComponent(item.id)}/cancel`, {method:'POST'})
    cancelMessage.value = result.message
    if (selected.value?.id === item.id && result.status === 'cancelling') selected.value = {...selected.value, cancel_requested:true, phase:'cancelling'}
    emit('cancelled')
    if (selected.value?.id === item.id) await loadDetail(item.id)
  } catch (error) { cancelError.value = describeError(error) }
  finally { cancelling.value.delete(item.id) }
}
</script>

<template>
  <p v-if="copyMessage" class="copy-message" role="status">{{ copyMessage }}</p>
  <p v-if="cancelMessage" class="copy-message" role="status">{{ cancelMessage }}</p>
  <p v-if="cancelError" class="cancel-error" role="alert">中断失败：{{ cancelError }}。请求状态请以刷新后的在途列表为准。</p>
  <div class="table-wrap">
    <table class="request-table">
      <thead><tr><th class="request-summary">请求内容 / 详情</th><th>时间</th><th>API Key</th><th>模型 / 接口</th><th>输入 / 输出 / 总计</th><th>结果与具体原因</th><th>耗时</th><th>请求 ID</th></tr></thead>
      <tbody>
        <tr v-if="!events.length"><td colspan="8" class="empty-row">{{ emptyText }}</td></tr>
        <tr v-for="item in events" :key="item.id">
          <td class="request-summary"><span>{{ requestSummary(item) }}</span><button class="detail-button" @click="open(item)">查看内容与详情</button><button v-if="item.status === 'in_flight' && item.cancel_supported" class="cancel-button" :disabled="item.cancel_requested || cancelling.has(item.id)" @click="cancelRequest(item)">{{ item.cancel_requested || cancelling.has(item.id) ? '中断处理中…' : '中断请求' }}</button><small v-else-if="item.status === 'in_flight'">此类请求暂不支持中断</small></td>
          <td class="time-cell">{{ date(item.created_at) }}</td>
          <td><strong>{{ item.key_name }}</strong><small>{{ item.request_metadata?.application || item.prefix }}</small><small v-if="item.request_metadata?.client_ip">来源 {{ item.request_metadata.client_ip }}</small></td>
          <td><code>{{ item.model }}</code><small>{{ item.endpoint }}</small></td>
          <td class="token-cell">
            <template v-if="item.status === 'in_flight'"><strong>处理中</strong><small>{{ item.request_metadata?.stream === true ? '流式 · Token 尚未结算' : item.request_metadata?.stream === false ? '非流式 · 完成后报告 Token' : '调用方式未记录' }}</small><small>完成时间未知</small></template>
            <template v-else-if="diagnostic(item).not_inferred"><strong>—</strong><small>未进入推理</small></template>
            <template v-else>{{ tokenText(item.input_tokens) }} / {{ tokenText(item.output_tokens) }} / {{ tokenText(item.total_tokens) }}<small>{{ usageQuality(item.usage_quality) }}</small></template>
          </td>
          <td class="reason-cell">
            <span :class="['result-pill', item.status === 'succeeded' ? 'ok' : item.status === 'in_flight' ? 'running' : item.status === 'cancelled' ? 'neutral' : 'warning']">{{ requestStatus(item.status) }}</span>
            <strong class="reason">{{ diagnostic(item).reason }}</strong>
            <small v-if="diagnostic(item).http_status">HTTP {{ diagnostic(item).http_status }}<template v-if="diagnostic(item).upstream_http_status && diagnostic(item).upstream_http_status !== diagnostic(item).http_status"> · 上游 {{ diagnostic(item).upstream_http_status }}</template></small>
            <small v-if="item.status !== 'succeeded' && item.status !== 'in_flight'" class="delivery">{{ delivery(item) }}</small>
            <small v-if="item.status !== 'succeeded' && diagnostic(item).suggestion" class="suggestion">建议：{{ diagnostic(item).suggestion }}</small>
          </td>
          <td>{{ duration(diagnostic(item).elapsed_ms) }}<small v-if="diagnostic(item).first_output_ms != null">首次输出 {{ duration(diagnostic(item).first_output_ms) }}</small></td>
          <td><button class="copy-id" :title="item.request_id + '（点击复制）'" :aria-label="'复制 Request ID ' + item.request_id" @click="copy(item.request_id)"><code>{{ item.request_id.length > 12 ? item.request_id.slice(0, 12) + '…' : item.request_id }}</code><Clipboard :size="12" /></button></td>
        </tr>
      </tbody>
    </table>
  </div>

  <div v-if="selected" class="detail-backdrop" @click.self="selected = null">
    <section ref="dialog" class="request-detail" role="dialog" aria-modal="true" aria-labelledby="request-detail-title" tabindex="-1" @keydown="dialogKey">
      <header><div><h2 id="request-detail-title">请求详情</h2><p>{{ selected.model }} · {{ selected.key_name }}</p></div><button aria-label="关闭请求详情" class="close-detail" autofocus @click="selected = null"><X :size="20" /></button></header>
      <p v-if="detailLoading" role="status" class="detail-note">正在加载请求详情…</p>
      <p v-if="detailError" role="alert" class="detail-error">详情加载失败：{{ detailError }} <button class="detail-button" @click="loadDetail(selected.id)">重试</button></p>
      <div v-if="selected.status === 'in_flight' && selected.cancel_supported" class="detail-cancel"><button class="cancel-button" :disabled="selected.cancel_requested || cancelling.has(selected.id)" @click="cancelRequest(selected)">{{ selected.cancel_requested || cancelling.has(selected.id) ? '中断处理中…' : '中断请求' }}</button><p>只中断此请求。应用自动重试可能重新占用名额；关闭上游连接前仍计入在途。</p><p v-if="cancelError" role="alert">{{ cancelError }}</p></div>
      <div :class="['outcome-box', selected.status === 'succeeded' && 'success']"><span>{{ requestStatus(selected.status) }}</span><h3>{{ diagnostic(selected).reason }}</h3><p>{{ delivery(selected) }}</p><p v-if="diagnostic(selected).suggestion">建议：{{ diagnostic(selected).suggestion }}</p></div>
      <section class="request-content-card" aria-labelledby="request-content-title">
        <div class="request-content-heading">
          <div><h3 id="request-content-title">本次调用的请求内容</h3><p>{{ selected.endpoint }}<template v-if="requestContent(selected).raw_bytes != null"> · 原始请求 {{ requestContent(selected).raw_bytes }} 字节</template></p></div>
          <button v-if="requestContent(selected).available" class="detail-button" @click="copyContent(selected)"><Clipboard :size="14" />复制内容</button>
        </div>
        <pre v-if="requestContent(selected).available">{{ requestContentText(selected) }}</pre>
        <p v-else class="content-unavailable">{{ requestContent(selected).reason || '请求内容未记录' }}</p>
        <div v-if="requestContent(selected).truncated || requestContent(selected).redacted_fields || requestContent(selected).binary_fields" class="content-flags">
          <span v-if="requestContent(selected).truncated">超长内容已截断</span>
          <span v-if="requestContent(selected).redacted_fields">{{ requestContent(selected).redacted_fields }} 个敏感字段已隐藏</span>
          <span v-if="requestContent(selected).binary_fields">{{ requestContent(selected).binary_fields }} 个二进制字段已省略</span>
        </div>
      </section>
      <dl class="detail-grid">
        <div><dt>来源 IP / 端口</dt><dd>{{ selected.request_metadata?.client_ip || '未记录' }}<template v-if="selected.request_metadata?.client_port"> : {{ selected.request_metadata.client_port }}</template></dd></div>
        <div><dt>应用归属</dt><dd>{{ selected.request_metadata?.application || '未设置' }}</dd></div>
        <div><dt>调用方式</dt><dd>{{ selected.request_metadata?.stream === true ? '流式输出' : selected.request_metadata?.stream === false ? '非流式，等待完整响应' : '未记录' }}</dd></div>
        <div><dt>消息数 / 工具定义数</dt><dd>{{ selected.request_metadata?.message_count ?? '未记录' }} / {{ selected.request_metadata?.tool_count ?? '未记录' }}</dd></div>
        <div><dt>客户端 User-Agent</dt><dd>{{ selected.request_metadata?.user_agent || '未记录' }}</dd></div>
        <div><dt>请求类型 / 大小</dt><dd>{{ selected.request_metadata?.content_type || '未记录' }} · {{ selected.request_metadata?.raw_bytes ?? '未记录' }} 字节</dd></div>
        <div><dt>发生环节 / 最后记录阶段</dt><dd>{{ stage(diagnostic(selected).stage) }}</dd></div>
        <div><dt>错误码</dt><dd><code>{{ diagnostic(selected).code || '—' }}</code></dd></div>
        <div><dt>HTTP 状态（响应头）</dt><dd>{{ diagnostic(selected).http_status ?? '未记录' }}</dd></div>
        <div><dt>上游 HTTP 状态</dt><dd>{{ diagnostic(selected).upstream_http_status ?? '未记录' }}</dd></div>
        <div><dt>当时并发 / 上限</dt><dd>{{ concurrency(diagnostic(selected)) }}</dd></div>
        <div><dt>限制范围</dt><dd>{{ ({model:'单模型',key:'单 Key',global:'整个网关',pool:'设备组'} as Record<string,string>)[diagnostic(selected).scope || ''] || '未记录' }}</dd></div>
        <div><dt>开始时间</dt><dd>{{ date(diagnostic(selected).started_at) }}</dd></div>
        <div><dt>结束 / 取消时间</dt><dd>{{ selected.status === 'in_flight' ? '尚未结束' : date(diagnostic(selected).finished_at) }}</dd></div>
        <div><dt>完整请求耗时</dt><dd>{{ duration(diagnostic(selected).elapsed_ms) }}</dd></div>
        <div><dt>首次输出耗时</dt><dd>{{ duration(diagnostic(selected).first_output_ms) }}</dd></div>
        <div><dt>最后输出距开始</dt><dd>{{ duration(diagnostic(selected).last_output_ms) }}</dd></div>
        <div><dt>模型结束原因</dt><dd>{{ diagnostic(selected).finish_reason ?? '未收到' }}</dd></div>
      </dl>
      <details v-if="selected.request_metadata?.effective_parameters" class="raw-usage"><summary>网关准备转交的生成参数（含默认值）</summary><pre>{{ JSON.stringify(selected.request_metadata.effective_parameters, null, 2) }}</pre></details>
      <p class="detail-note">请求正文仅向管理员展示。敏感字段会隐藏，文件二进制内容不保存，超长文本明确标注截断；历史未记录内容无法补回。模型输出正文未存入此账本。</p>
      <p class="detail-note">首次输出包含正文、思考或工具调用片段，不等于引擎内部 TTFT。HTTP 200 仅表示响应头成功，流式结果以最终状态为准。</p>
      <p v-if="selected.status === 'cancelled' && diagnostic(selected).code !== 'admin_cancelled'" class="detail-note">此记录不能区分手动停止、应用超时或网络断开，也不能单独证明模型已经停止计算。</p>
      <p v-if="diagnostic(selected).source === 'historical_access_rejection'" class="detail-note">原因关联自历史拒绝记录。HTTP 状态按该错误码的固定映射补充；未保存的并发快照与耗时保持“未记录”。</p>
      <div class="request-reference"><strong>Request ID</strong><code>{{ selected.request_id }}</code><button class="detail-button" @click="copy(selected.request_id)">复制</button></div>
      <p class="detail-note">内部记录 ID：{{ selected.id }}</p>
      <details class="raw-usage"><summary>Token 用量与原始计数（{{ diagnostic(selected).not_inferred ? '未进入推理，不适用' : usageQuality(selected.usage_quality) }}）</summary><pre>{{ JSON.stringify({ input: selected.input_tokens, output: selected.output_tokens, total: selected.total_tokens, source: selected.usage_source, raw: selected.usage_raw, issues: selected.usage_issues }, null, 2) }}</pre></details>
    </section>
  </div>
</template>

<style scoped>
.cancel-button{display:block;margin-top:10px;padding:5px 9px;border:1px solid #e2baba;border-radius:6px;background:#fff6f4;color:#a23e30;cursor:pointer;font:inherit;font-size:12px}.cancel-button:disabled{opacity:.55;cursor:wait}.cancel-error{margin:10px 20px;color:#a23e30;font-size:12px}.detail-cancel{margin:12px 0 20px;padding:12px;background:#fff8f5;border-radius:8px}.detail-cancel p{font-size:12px;line-height:1.7;color:#885a4d}
.request-summary .detail-button{display:block;margin-top:9px;padding:0;text-align:left}
.running{background:#e7f2fc;color:#246391}.detail-error{font-size:13px;color:#a14343;line-height:1.7}
.request-table{width:100%;font-size:12px}.request-table td{vertical-align:middle;padding:16px 12px}.request-table small{display:block;color:#71818a;font-size:11px;line-height:1.5;margin-top:5px}.time-cell{min-width:155px;max-width:175px;white-space:nowrap}.request-summary{min-width:190px;max-width:280px}.request-summary span{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;line-height:1.55;overflow-wrap:anywhere}.token-cell{min-width:125px}.reason-cell{min-width:245px;max-width:350px}.reason{display:block;margin-top:7px;font-size:12px}.reason-cell .delivery{color:#596d76}.reason-cell .suggestion{color:#775b20}.result-pill{display:inline-block;border-radius:20px;padding:4px 9px;font-size:11px;font-weight:600}.ok{background:#e5f4ed;color:#176449}.warning{background:#fff2dd;color:#875d16}.neutral{background:#edf0f4;color:#52616d}.detail-button,.copy-id,.close-detail{border:0;cursor:pointer;background:transparent;color:#18755a;font:inherit}.detail-button{font-weight:600;white-space:nowrap}.copy-id{display:flex;align-items:center;gap:6px;font-size:11px}.copy-message{margin:8px 20px;color:#18755a;font-size:12px}.detail-backdrop{position:fixed;inset:0;background:#142b3b66;z-index:1000;display:flex;justify-content:flex-end}.request-detail{width:min(700px,100%);height:100%;overflow:auto;background:#fff;padding:28px;box-sizing:border-box;box-shadow:-8px 0 40px #142b3b22;text-align:left}.request-detail header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:22px}.request-detail h2{font-size:21px;margin:0}.request-detail header p{font-size:13px;color:#687b87;margin:8px 0}.outcome-box{background:#fff8ea;border:1px solid #eadab6;border-radius:12px;padding:18px}.outcome-box.success{background:#edf8f2;border-color:#cce4d6}.outcome-box>span{font-size:12px;color:#7a6328}.outcome-box h3{font-size:19px;margin:8px 0 12px}.outcome-box p{font-size:13px;line-height:1.7;margin:6px 0}.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 22px;margin:22px 0}.detail-grid>div{padding:12px 0;border-bottom:1px solid #eaf0f3;min-width:0}.detail-grid dt{font-size:12px;color:#71818a}.detail-grid dd{margin:7px 0 0;font-size:13px;overflow-wrap:anywhere}.detail-note{font-size:12px;color:#71818a;line-height:1.8;overflow-wrap:anywhere}.request-reference{display:flex;flex-wrap:wrap;align-items:center;gap:12px;font-size:12px;margin-top:20px}.request-reference code{overflow-wrap:anywhere;user-select:all}.raw-usage{margin:22px 0;font-size:12px}.raw-usage summary{cursor:pointer;color:#526879}.raw-usage pre{background:#f4f7f9;padding:15px;max-height:300px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}
.request-content-card{margin-top:18px;border:1px solid #d9e7e2;border-radius:12px;background:#f7fbf9;padding:16px}.request-content-heading{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}.request-content-heading h3{font-size:15px;margin:0}.request-content-heading p{font-size:11px;color:#71818a;margin:6px 0 0}.request-content-heading .detail-button{display:flex;align-items:center;gap:6px}.request-content-card pre{margin:14px 0 0;background:#102b2a;color:#e9faf4;border-radius:8px;padding:14px;max-height:380px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;line-height:1.6;user-select:text}.content-unavailable{margin:14px 0 0;color:#71818a;font-size:12px}.content-flags{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px}.content-flags span{font-size:10px;color:#775b20;background:#fff2d8;border-radius:20px;padding:4px 8px}
@media(max-width:600px){.request-detail{padding:18px}.detail-grid{grid-template-columns:1fr}}
</style>
