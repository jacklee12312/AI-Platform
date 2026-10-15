<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Braces, Check, Clock3, Copy, Download, KeyRound, Radio, RefreshCw, Search, ShieldAlert } from '@lucide/vue'
import { api, describeError } from '../api'

interface Field { name: string; type: string; required: boolean; description: string; constraints: string; location: string; example?: unknown }
interface GatewayError { status: number; code: string; meaning: string; action: string }
interface Timeout { id: string; label: string; value: number; default: number; unit: string; source: string; confirmed: boolean }
interface Protocol { id: string; name: string; summary: string; completion: string }
interface RequestExample { id: string; title: string; method: string; path: string; transport: string; note: string; snippets: Record<string, string> }
interface Endpoint {
  id: string; group: string; method: string; path: string; aliases: string[]; title: string; summary: string;
  auth: boolean; content_type: string | null; response_content_type: string; timeout: string | null;
  timeout_value: Timeout | null; models: string[]; models_confirmed: boolean; request_fields: Field[];
  response_fields: Field[]; streaming: null | { protocol: string; enabled_by: string; events: string[]; rules: string[] };
  errors: GatewayError[]; notes: string[]; examples: RequestExample[];
}
interface Reference {
  generated_at: string; base_url: string; registry_available: boolean; config_available: boolean; notice: string;
  authentication: { header: string; description: string }; request_id: string;
  error_shape: { example: unknown; rules: string[] }; protocols: Protocol[]; timeouts: Timeout[];
  errors: GatewayError[]; endpoints: Endpoint[];
}

const route = useRoute(), router = useRouter()
const reference = ref<Reference | null>(null), loading = ref(false), error = ref('')
const tab = ref<'endpoints' | 'protocols' | 'errors'>('endpoints'), query = ref(''), group = ref('全部')
const selectedId = ref('')
const exampleId = ref(''), language = ref('python'), copied = ref(false), copyMessage = ref('')
const codeArea = ref<HTMLTextAreaElement | null>(null)
const groups = computed(() => ['全部', ...new Set((reference.value?.endpoints || []).map(item => item.group))])
const filtered = computed(() => (reference.value?.endpoints || []).filter(item => {
  const text = `${item.method} ${item.path} ${item.aliases.join(' ')} ${item.title} ${item.summary} ${item.models.join(' ')}`.toLowerCase()
  return (group.value === '全部' || item.group === group.value) && text.includes(query.value.trim().toLowerCase())
}))
const selected = computed(() => reference.value?.endpoints.find(item => item.id === selectedId.value) || filtered.value[0])
const requestExample = computed(() => selected.value?.examples.find(item => item.id === exampleId.value) || selected.value?.examples[0])
const requestCode = computed(() => requestExample.value?.snippets[language.value] || '')
const languages: Record<string, string> = { python: 'Python', bash: 'curl · Bash', powershell: 'PowerShell · curl.exe' }
const errorRows = computed(() => (reference.value?.errors || []).filter(item =>
  `${item.status} ${item.code} ${item.meaning} ${item.action}`.toLowerCase().includes(query.value.trim().toLowerCase())))

function choose(id: string) {
  selectedId.value = id
  router.replace({ query: { endpoint: id } })
}
function show(next: typeof tab.value) {
  tab.value = next; query.value = ''
}
function example(value: unknown) {
  if (value === undefined) return '—'
  return typeof value === 'string' ? value : JSON.stringify(value, null, 0)
}
function timeout(item: Endpoint) {
  if (!item.timeout_value) return '未设统一客户端时限'
  return `${item.timeout_value.value} ${item.timeout_value.unit}${item.timeout_value.confirmed ? ' · 当前保存配置' : ' · 源码默认，运行值待确认'}`
}
async function copyCode() {
  copied.value = false; copyMessage.value = ''
  try {
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(requestCode.value)
    else {
      codeArea.value?.focus(); codeArea.value?.select()
      if (!document.execCommand('copy')) throw new Error('clipboard unavailable')
    }
    copied.value = true; copyMessage.value = '请求示例已复制；执行前请设置自己的 API_KEY，并准备接口所需文件。'
  } catch {
    codeArea.value?.focus(); codeArea.value?.select()
    copyMessage.value = '浏览器未允许自动复制，代码已选中，请按 Ctrl+C（macOS 为 ⌘C）。'
  }
}
async function load() {
  if (loading.value) return
  loading.value = true; error.value = ''
  try {
    reference.value = await api<Reference>('/api/api-reference')
    const requested = typeof route.query.endpoint === 'string' ? route.query.endpoint : ''
    selectedId.value = reference.value.endpoints.some(item => item.id === requested) ? requested : reference.value.endpoints[0]?.id || ''
  } catch (reason) { reference.value = null; error.value = describeError(reason) }
  finally { loading.value = false }
}
watch(filtered, items => {
  if (items.length && !items.some(item => item.id === selectedId.value)) selectedId.value = items[0].id
})
watch(selected, value => {
  exampleId.value = value?.examples[0]?.id || ''; language.value = 'python'; copied.value = false; copyMessage.value = ''
})
watch(requestExample, value => { if (value && !value.snippets[language.value]) language.value = 'python' })
watch(requestCode, () => { copied.value = false; copyMessage.value = '' })
onMounted(load)
</script>

<template>
  <div class="reference-page page-body">
    <section class="reference-hero">
      <div><p class="eyebrow">PUBLIC GATEWAY CONTRACT</p><h2>从字段到失败处理，一处完成接入</h2><p>{{ reference?.notice || '查看公共接口合同、流式结束条件、当前超时和错误码。' }}</p></div>
      <a class="primary-button" href="/api/api-reference/openapi.json" download><Download :size="16" />下载 OpenAPI</a>
    </section>

    <p v-if="error" class="notice error-notice" role="alert">{{ error }}</p>
    <p v-if="reference && !reference.registry_available" class="notice error-notice" role="alert">模型注册表暂不可读取，接口合同仍可查看，支持模型列表标为未确认。</p>

    <section v-if="reference" class="reference-basics">
      <article><Braces :size="18"/><div><span>默认网关地址</span><code>{{ reference.base_url }}</code></div></article>
      <article><KeyRound :size="18"/><div><span>业务鉴权</span><code>{{ reference.authentication.header }}</code></div></article>
      <article><Clock3 :size="18"/><div><span>请求追踪</span><p>{{ reference.request_id }}</p></div></article>
    </section>

    <div class="reference-tabs" role="tablist" aria-label="API 参考内容">
      <button role="tab" :aria-selected="tab === 'endpoints'" :class="{ active: tab === 'endpoints' }" @click="show('endpoints')">接口清单</button>
      <button role="tab" :aria-selected="tab === 'protocols'" :class="{ active: tab === 'protocols' }" @click="show('protocols')">流式与超时</button>
      <button role="tab" :aria-selected="tab === 'errors'" :class="{ active: tab === 'errors' }" @click="show('errors')">错误码</button>
    </div>

    <template v-if="reference && tab === 'endpoints'">
      <section class="panel reference-toolbar">
        <label><Search :size="16"/><input v-model="query" aria-label="搜索 API 接口" placeholder="搜索路径、用途或模型" /></label>
        <select v-model="group" aria-label="按 API 分组筛选"><option v-for="item in groups" :key="item">{{ item }}</option></select>
        <button class="secondary-button" :disabled="loading" @click="load"><RefreshCw :size="14" :class="{ spin: loading }"/>刷新</button>
      </section>
      <div class="reference-layout">
        <nav class="panel endpoint-list" aria-label="公共 API 接口">
          <button v-for="item in filtered" :key="item.id" :class="{ active: selected?.id === item.id }" :aria-label="`查看 ${item.method} ${item.path}`" @click="choose(item.id)">
            <span :data-method="item.method">{{ item.method }}</span><div><code>{{ item.path }}</code><small>{{ item.title }}</small></div>
          </button>
          <p v-if="!filtered.length">没有匹配的接口</p>
        </nav>

        <article v-if="selected" class="panel endpoint-detail">
          <header><div><span class="method-chip" :data-method="selected.method">{{ selected.method }}</span><h2>{{ selected.title }}</h2><code>{{ selected.path }}</code></div><span class="endpoint-group">{{ selected.group }}</span></header>
          <p class="endpoint-summary">{{ selected.summary }}</p>
          <div class="endpoint-facts">
            <div><span>鉴权</span><strong>{{ selected.auth ? 'Bearer API Key' : '无需鉴权' }}</strong></div>
            <div><span>请求类型</span><strong>{{ selected.content_type || '无请求体' }}</strong></div>
            <div><span>响应类型</span><strong>{{ selected.response_content_type }}</strong></div>
            <div><span>服务端时限</span><strong>{{ timeout(selected) }}</strong></div>
          </div>
          <div v-if="selected.aliases.length" class="alias-row"><strong>兼容路径</strong><code v-for="item in selected.aliases" :key="item">{{ item }}</code></div>
          <div class="model-row"><strong>支持模型</strong><p v-if="selected.models.length"><code v-for="item in selected.models" :key="item">{{ item }}</code></p><span v-else>{{ selected.models_confirmed ? '此接口不绑定单一模型列表' : '注册表不可用，暂未确认' }}</span></div>

          <section v-if="selected.examples.length" class="request-example">
            <div class="request-example-head"><div><h3>可复制请求示例</h3><p>与其他模型一致，从环境变量读取业务 API_KEY；可用 GATEWAY_URL 覆盖当前网关地址。</p></div><span>公共网关 · 原始文件上传</span></div>
            <div class="request-example-controls"><label>调用方式<select v-model="exampleId" aria-label="示例调用方式"><option v-for="item in selected.examples" :key="item.id" :value="item.id">{{ item.title }}</option></select></label><label>示例语言<select v-model="language" aria-label="示例语言"><option v-for="(_, key) in requestExample?.snippets" :key="key" :value="key">{{ languages[key] || key }}</option></select></label><button class="secondary-button" @click="copyCode"><Check v-if="copied" :size="14"/><Copy v-else :size="14"/>{{ copied ? '已复制' : '复制请求' }}</button></div>
            <p v-if="requestExample?.note" class="request-example-note">{{ requestExample.note }}</p>
            <code class="request-endpoint">{{ requestExample?.method }} {{ requestExample?.path }}</code>
            <textarea ref="codeArea" :value="requestCode" readonly spellcheck="false" aria-label="可复制请求代码" class="request-code"></textarea>
            <p v-if="copyMessage" role="status" class="request-copy-message">{{ copyMessage }}</p>
          </section>

          <section class="field-section"><h3>请求字段</h3><div class="field-table-wrap"><table><thead><tr><th>位置 / 字段</th><th>类型</th><th>要求</th><th>说明</th><th>示例</th></tr></thead><tbody><tr v-for="item in selected.request_fields" :key="`${item.location}-${item.name}`"><td><small>{{ item.location }}</small><code>{{ item.name }}</code></td><td><code>{{ item.type }}</code></td><td><span :class="['requirement', item.required && 'required']">{{ item.required ? '必填' : '可选' }}</span><small v-if="item.constraints">{{ item.constraints }}</small></td><td>{{ item.description }}</td><td><code>{{ example(item.example) }}</code></td></tr><tr v-if="!selected.request_fields.length"><td colspan="5">此接口没有请求字段。</td></tr></tbody></table></div></section>
          <section class="field-section"><h3>成功响应</h3><div class="field-table-wrap"><table><thead><tr><th>字段</th><th>类型</th><th>要求</th><th>说明</th></tr></thead><tbody><tr v-for="item in selected.response_fields" :key="item.name"><td><code>{{ item.name }}</code></td><td><code>{{ item.type }}</code></td><td><span :class="['requirement', item.required && 'required']">{{ item.required ? '固定/必有' : '按格式返回' }}</span></td><td>{{ item.description }}</td></tr></tbody></table></div></section>
          <section v-if="selected.streaming" class="stream-contract"><Radio :size="20"/><div><h3>{{ selected.streaming.protocol.toUpperCase() }} 协议</h3><p>{{ selected.streaming.enabled_by }}</p><div><code v-for="item in selected.streaming.events" :key="item">{{ item }}</code></div><ul><li v-for="item in selected.streaming.rules" :key="item">{{ item }}</li></ul></div></section>
          <section class="endpoint-errors"><h3>本接口可能返回的错误</h3><div><span v-for="item in selected.errors" :key="item.code"><b>{{ item.status }}</b><code>{{ item.code }}</code><small>{{ item.meaning }}</small></span></div></section>
          <ul v-if="selected.notes.length" class="endpoint-notes"><li v-for="item in selected.notes" :key="item">{{ item }}</li></ul>
        </article>
      </div>
    </template>

    <template v-if="reference && tab === 'protocols'">
      <section class="protocol-grid"><article v-for="item in reference.protocols" :key="item.id" class="panel"><Radio :size="20"/><h3>{{ item.name }}</h3><p>{{ item.summary }}</p><strong>完成判据</strong><p>{{ item.completion }}</p></article></section>
      <section class="panel timeout-panel"><header><div><h2>当前超时与连接上限</h2><p>显示保存配置；未读到配置时明确使用源码默认值。</p></div><Clock3 :size="20"/></header><div><article v-for="item in reference.timeouts" :key="item.id"><div><strong>{{ item.label }}</strong><code>{{ item.id }}</code></div><b>{{ item.value }} {{ item.unit }}</b><span :class="{ confirmed: item.confirmed }">{{ item.confirmed ? '当前保存配置' : '源码默认 · 运行值待确认' }}</span></article></div></section>
      <section class="panel stream-warning"><ShieldAlert :size="20"/><div><strong>客户端超时应大于网关时限，并保留取消后的不确定状态</strong><p>连接断开不一定终止后台推理。具有副作用或结果不可安全重放的业务，应先按 X-Request-ID 查询审计，再决定是否重试。</p></div></section>
    </template>

    <template v-if="reference && tab === 'errors'">
      <section class="panel reference-toolbar"><label><Search :size="16"/><input v-model="query" aria-label="搜索 API 错误码" placeholder="搜索 HTTP 状态、错误码或处理方式" /></label></section>
      <section class="panel error-contract"><header><h2>统一错误结构</h2><p>HTTP JSON 错误均包含 error 和 request_id；流式请求还需检查 SSE / WebSocket 错误事件。</p></header><pre>{{ JSON.stringify(reference.error_shape.example, null, 2) }}</pre><ul><li v-for="item in reference.error_shape.rules" :key="item">{{ item }}</li></ul></section>
      <section class="panel error-table"><div class="field-table-wrap"><table><thead><tr><th>HTTP</th><th>error.code</th><th>含义</th><th>调用方处理</th></tr></thead><tbody><tr v-for="item in errorRows" :key="item.code"><td><b>{{ item.status }}</b></td><td><code>{{ item.code }}</code></td><td>{{ item.meaning }}</td><td>{{ item.action }}</td></tr></tbody></table></div></section>
    </template>

    <p v-if="loading && !reference" class="reference-loading">正在生成 API 参考…</p>
  </div>
</template>

<style scoped>
.request-example{margin:18px 20px 2px;padding:16px;border:1px solid #c5dfd5;border-radius:8px;background:#f4faf7}.request-example-head{display:flex;justify-content:space-between;gap:12px}.request-example-head h3{font-size:12px}.request-example-head p{margin-top:5px;color:var(--muted);font-size:9px;line-height:1.6}.request-example-head>span{color:#507469;font-size:9px;white-space:nowrap}.request-example-controls{display:flex;align-items:flex-end;gap:9px;flex-wrap:wrap;margin:13px 0}.request-example-controls label{display:grid;gap:5px;color:var(--muted);font-size:9px}.request-example-controls select{min-height:32px;padding:0 26px 0 8px;border:1px solid var(--line);border-radius:6px;background:white;color:var(--ink);font-size:10px}.request-example-note{margin-bottom:10px;color:#7a612f;font-size:9px;line-height:1.7}.request-endpoint{display:block;padding:9px 11px;border-radius:6px 6px 0 0;background:#dfeee7;color:#315f50;font-size:9px;overflow-wrap:anywhere}.request-code{display:block;width:100%;height:280px;resize:vertical;padding:14px;border:0;border-radius:0 0 6px 6px;background:#12282d;color:#d6ede4;font:10px/1.75 Consolas,monospace;white-space:pre;overflow:auto}.request-copy-message{margin-top:8px;color:#28724e;font-size:9px}
.reference-page{gap:16px}.reference-hero{display:flex;align-items:center;justify-content:space-between;gap:20px;padding:22px 24px;border-radius:12px;color:#eef9f6;background:linear-gradient(125deg,#102a31,#195044)}.reference-hero h2{font-size:23px}.reference-hero p:last-child{margin-top:8px;color:#a9c8c1;font-size:12px}.reference-hero .eyebrow{color:#77bea7}.reference-hero .primary-button{background:#c7f1df;color:#143e35;text-decoration:none;box-shadow:none}.reference-basics{display:grid;grid-template-columns:1fr 1.25fr 1.4fr;gap:12px}.reference-basics article{display:flex;align-items:flex-start;gap:10px;padding:14px 16px;border:1px solid var(--line);border-radius:10px;background:white}.reference-basics svg{color:#288264}.reference-basics span,.reference-basics code,.reference-basics p{display:block}.reference-basics span{color:var(--muted);font-size:9px}.reference-basics code,.reference-basics p{margin-top:5px;font-size:10px;line-height:1.55;overflow-wrap:anywhere}.reference-tabs{display:flex;gap:5px;padding:5px;border:1px solid var(--line);border-radius:10px;background:#ffffffa8}.reference-tabs button{flex:1;min-height:38px;border:0;border-radius:7px;color:#74848a;background:transparent;font-size:11px;font-weight:700}.reference-tabs button.active{color:#165e4b;background:white;box-shadow:0 2px 9px #1a363b12}.reference-toolbar{display:flex;align-items:center;gap:10px;padding:11px}.reference-toolbar label{flex:1;display:flex;align-items:center;gap:8px;padding:0 10px}.reference-toolbar input{width:100%;height:32px;border:0;outline:0;background:transparent;font-size:11px}.reference-toolbar select{height:34px;padding:0 27px 0 9px;border:1px solid var(--line);border-radius:7px;background:white;color:#52636a;font-size:10px}.reference-layout{display:grid;grid-template-columns:290px minmax(0,1fr);gap:14px;align-items:start}.endpoint-list{position:sticky;top:16px;max-height:calc(100vh - 32px);overflow:auto;padding:7px}.endpoint-list button{width:100%;display:grid;grid-template-columns:45px 1fr;gap:9px;align-items:start;padding:10px;border:0;border-radius:7px;background:transparent;text-align:left}.endpoint-list button:hover,.endpoint-list button.active{background:#edf6f2}.endpoint-list button>span,.method-chip{display:inline-flex;justify-content:center;padding:4px 5px;border-radius:5px;color:#17624e;background:#dff2e9;font:700 8px 'DM Mono',monospace}.endpoint-list [data-method=GET],.method-chip[data-method=GET]{color:#285f8a;background:#e2eef7}.endpoint-list [data-method=WS],.method-chip[data-method=WS]{color:#7b4f99;background:#f0e5f7}.endpoint-list code,.endpoint-list small{display:block}.endpoint-list code{font-size:9px;overflow-wrap:anywhere}.endpoint-list small{margin-top:4px;color:var(--muted);font-size:8px}.endpoint-list>p{padding:20px;color:var(--muted);font-size:10px}.endpoint-detail{overflow:hidden}.endpoint-detail>header{display:flex;justify-content:space-between;align-items:flex-start;padding:20px;border-bottom:1px solid #edf0f1}.endpoint-detail>header>div{display:grid;grid-template-columns:auto 1fr;align-items:center;gap:9px}.endpoint-detail h2{font-size:17px}.endpoint-detail header code{grid-column:1/-1;color:#597169;font-size:11px}.endpoint-group{padding:5px 8px;border-radius:999px;color:#617871;background:#edf4f2;font-size:9px}.endpoint-summary{padding:16px 20px 0;color:#53666c;font-size:12px;line-height:1.75}.endpoint-facts{display:grid;grid-template-columns:repeat(4,1fr);margin:16px 20px;border:1px solid var(--line);border-radius:8px}.endpoint-facts>div{padding:11px}.endpoint-facts>div+div{border-left:1px solid var(--line)}.endpoint-facts span,.endpoint-facts strong{display:block}.endpoint-facts span{color:var(--muted);font-size:8px}.endpoint-facts strong{margin-top:5px;font-size:9px;line-height:1.5}.alias-row,.model-row{margin:10px 20px;display:flex;align-items:flex-start;gap:10px;font-size:10px}.alias-row code,.model-row code{display:inline-block;margin:0 5px 5px 0;padding:4px 6px;border-radius:4px;background:#edf4f2;color:#42685d;font-size:8px}.model-row>strong,.alias-row>strong{flex:0 0 78px}.model-row p{flex:1}.model-row>span{color:var(--muted)}.field-section{padding:18px 20px 0}.field-section h3,.endpoint-errors h3{margin-bottom:10px;font-size:12px}.field-table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:7px}.field-table-wrap table{min-width:660px}.field-table-wrap th{padding:8px 10px}.field-table-wrap td{padding:9px 10px;vertical-align:top;line-height:1.55}.field-table-wrap td code{font-size:8px;overflow-wrap:anywhere}.field-table-wrap td small{display:block;color:var(--muted);font-size:8px}.requirement{display:inline-block;padding:3px 5px;border-radius:4px;background:#edf0f1;color:#65767c;font-size:8px}.requirement.required{color:#9b4b42;background:#f8e8e5}.stream-contract,.stream-warning{display:flex;gap:12px;margin:18px 20px;padding:15px;border:1px solid #c5dfd5;border-radius:8px;background:#f1faf6}.stream-contract svg,.stream-warning svg{flex:none;color:#23765c}.stream-contract h3{font-size:12px}.stream-contract p{margin-top:4px;color:var(--muted);font-size:9px}.stream-contract>div>div{display:flex;flex-wrap:wrap;gap:5px;margin:10px 0}.stream-contract code{padding:4px 6px;border-radius:4px;background:white;font-size:8px}.stream-contract li,.endpoint-notes li{color:#586b63;font-size:9px;line-height:1.7}.endpoint-errors{padding:18px 20px}.endpoint-errors>div{display:grid;grid-template-columns:repeat(2,1fr);gap:7px}.endpoint-errors span{display:grid;grid-template-columns:30px auto 1fr;align-items:center;gap:6px;padding:8px;border-radius:6px;background:#faf4f1}.endpoint-errors b{font-size:8px;color:#9b554d}.endpoint-errors code{font-size:8px}.endpoint-errors small{color:#756d6a;font-size:8px}.endpoint-notes{margin:0 20px 20px;padding:13px 13px 13px 28px;border-radius:7px;background:#fff9ed}.protocol-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:13px}.protocol-grid article{padding:18px}.protocol-grid svg{color:#2b8065}.protocol-grid h3{margin:10px 0 7px;font-size:14px}.protocol-grid p{color:#6d7d82;font-size:10px;line-height:1.7}.protocol-grid strong{display:block;margin:13px 0 4px;font-size:9px}.timeout-panel{overflow:hidden}.timeout-panel>header,.error-contract>header{display:flex;justify-content:space-between;padding:17px 19px;border-bottom:1px solid var(--line)}.timeout-panel header p,.error-contract header p{margin-top:4px;color:var(--muted);font-size:9px}.timeout-panel>div{display:grid;grid-template-columns:repeat(2,1fr)}.timeout-panel article{display:grid;grid-template-columns:1fr auto;gap:6px;padding:14px 18px;border-top:1px solid #edf0f1}.timeout-panel article:nth-child(odd){border-right:1px solid #edf0f1}.timeout-panel article:nth-child(-n+2){border-top:0}.timeout-panel article strong,.timeout-panel article code{display:block}.timeout-panel article strong{font-size:10px}.timeout-panel article code{margin-top:4px;color:var(--muted);font-size:8px}.timeout-panel article b{font-size:12px}.timeout-panel article span{grid-column:2;color:#9b6b2e;font-size:8px}.timeout-panel article span.confirmed{color:#287257}.stream-warning{margin:0}.stream-warning strong{font-size:11px}.stream-warning p{margin-top:5px;color:#5f756c;font-size:9px;line-height:1.7}.error-contract{display:grid;grid-template-columns:1fr minmax(260px,.7fr);overflow:hidden}.error-contract>header{display:block}.error-contract pre{grid-row:1/3;grid-column:2;margin:0;padding:18px;color:#cce7df;background:#132d34;font:9px/1.7 Consolas,monospace;overflow:auto}.error-contract ul{margin:0;padding:14px 18px 14px 36px}.error-contract li{font-size:9px;line-height:1.8;color:#5f706a}.error-table{overflow:hidden}.error-table b{font-size:9px}.reference-loading{padding:40px;text-align:center;color:var(--muted);font-size:11px}@media(max-width:1100px){.reference-layout{grid-template-columns:240px minmax(0,1fr)}.endpoint-facts{grid-template-columns:repeat(2,1fr)}.endpoint-facts>div:nth-child(3){border-left:0;border-top:1px solid var(--line)}.endpoint-facts>div:nth-child(4){border-top:1px solid var(--line)}}@media(max-width:760px){.reference-hero{align-items:flex-start;flex-direction:column}.reference-basics,.reference-layout{grid-template-columns:1fr}.reference-toolbar{flex-wrap:wrap}.reference-toolbar label{flex-basis:100%}.endpoint-list{position:static;max-height:300px}.endpoint-detail>header{gap:10px}.endpoint-facts{grid-template-columns:1fr}.endpoint-facts>div+div{border-left:0;border-top:1px solid var(--line)}.endpoint-errors>div,.protocol-grid,.timeout-panel>div{grid-template-columns:1fr}.timeout-panel article:nth-child(odd){border-right:0}.timeout-panel article:nth-child(2){border-top:1px solid #edf0f1}.error-contract{grid-template-columns:1fr}.error-contract pre{grid-row:auto;grid-column:auto}.reference-tabs{overflow-x:auto}.reference-tabs button{flex:1 0 110px}}
</style>
