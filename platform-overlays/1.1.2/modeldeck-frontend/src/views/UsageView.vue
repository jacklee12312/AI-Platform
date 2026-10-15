<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import {
  Ban,
  Check,
  Clipboard,
  KeyRound,
  LoaderCircle,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  Trash2,
  X,
} from '@lucide/vue'
import { api, describeError } from '../api'
import { requestStatus, dateBoundaries } from '../usagePresentation'
import KeyPolicyFields from '../KeyPolicyFields.vue'
import ResourceControls from '../ResourceControls.vue'
import ActiveRequests from '../ActiveRequests.vue'
import UsageRequests, { type UsageEvent } from '../UsageRequests.vue'
import { defaultKeyPolicy, normalizePolicy, type KeyPolicy, type ResourceSnapshot } from '../keyPolicy'

interface ApiKeyRecord {
  id: string
  name: string
  prefix: string
  created_at: string
  last_used_at: string | null
  enabled: boolean
  revoked_at: string | null
  request_count: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  policy: KeyPolicy
  access: { expired: boolean; daily_requests: number; daily_remaining: number | null; monthly_known_tokens: number; monthly_uncertain_requests: number; token_warning_reached: boolean }
}

interface UsageAggregate {
  api_key_id?: string
  name?: string
  prefix?: string
  model?: string
  request_count: number
  input_tokens: number
  output_tokens: number
  total_tokens: number
  unreported_requests?: number
  anomaly_requests?: number
  missing_total_tokens_requests?: number
}

interface UsageReport {
  reason_options?: { code: string; label: string }[]
  summary: {
    request_count: number
    input_tokens: number
    output_tokens: number
    total_tokens: number
    unreported_requests: number
    admission_rejected_requests?: number
    anomaly_requests?: number
    legacy_requests?: number
    missing_input_tokens_requests?: number
    missing_output_tokens_requests?: number
    missing_total_tokens_requests?: number
  }
  by_key: UsageAggregate[]
  by_model: UsageAggregate[]
  daily: UsageAggregate[]
  recent: UsageEvent[]
  pagination?: { offset: number; limit: number; total: number; has_more: boolean }
}

interface CreatedKey {
  id: string
  name: string
  prefix: string
  key: string
  created_at: string
  enabled: boolean
}

const keys = ref<ApiKeyRecord[]>([])
const showRevoked = ref(false)
const visibleKeys = computed(() => keys.value.filter(item => showRevoked.value || !item.revoked_at))
const revokedCount = computed(() => keys.value.filter(item => item.revoked_at).length)
const resources = ref<ResourceSnapshot | null>(null)
const createPolicy = ref<KeyPolicy>(defaultKeyPolicy())
const policyTarget = ref<ApiKeyRecord | null>(null)
const editPolicy = ref<KeyPolicy>(defaultKeyPolicy())
const report = ref<UsageReport | null>(null)
const loading = ref(false)
const error = ref('')
const selectedKey = ref('')
const startDate = ref('')
const endDate = ref('')
const createOpen = ref(false)
const createName = ref('')
const creating = ref(false)
const createdKey = ref<CreatedKey | null>(null)
const copied = ref(false)
const revokeTarget = ref<ApiKeyRecord | null>(null)
const mutatingId = ref('')
const modelFilter = ref('')
const route = useRoute()
const requestFilter = ref(typeof route.query.request_id === 'string' ? route.query.request_id : '')
const requestDetails = ref<HTMLElement | null>(null)
function showRequests() { requestDetails.value?.scrollIntoView({ block: 'start' }) }
const statusFilter = ref('')
const reasonFilter = ref('')
const exporting = ref(false)
const offset = ref(0)
const message = ref('')
const renameTarget = ref<ApiKeyRecord | null>(null)
const renameName = ref('')
const appliedFilter = ref('')
const filterSignature = computed(() => JSON.stringify([selectedKey.value, startDate.value, endDate.value, modelFilter.value, requestFilter.value, statusFilter.value, reasonFilter.value]))
let revision = 0

const activeKeys = computed(() => keys.value.filter((item) => item.enabled && !item.revoked_at && !item.access?.expired).length)

function formatNumber(value: number | undefined) {
  return value == null ? '—' : new Intl.NumberFormat('zh-CN').format(value)
}

function formatDate(value: string | null) {
  if (!value) return '尚未调用'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).format(new Date(value))
}

function usageParams() {
  const dates = dateBoundaries(startDate.value, endDate.value)
  const params = new URLSearchParams()
  for (const [name, value] of Object.entries({
    api_key_id: selectedKey.value,
    start: dates.start,
    end: dates.end,
    model: modelFilter.value.trim(),
    request_id: requestFilter.value.trim(),
    status: statusFilter.value,
    error_code: reasonFilter.value,
  })) {
    if (value) params.set(name, value)
  }
  return params
}

async function exportUsage() {
  exporting.value = true
  error.value = ''
  try {
    const csv = await api<string>(`/api/usage/export?${usageParams()}`)
    const url = URL.createObjectURL(new Blob(['\ufeff', csv.replace(/^\ufeff/, '')], { type: 'text/csv;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = 'modeldeck-usage.csv'
    document.body.appendChild(link)
    link.click()
    link.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
    message.value = '已导出全部匹配记录（不限于当前页），CSV 时间为 UTC。'
  } catch (value) {
    error.value = describeError(value)
  } finally {
    exporting.value = false
  }
}

async function load(reset = true) {
  if (reset || appliedFilter.value !== filterSignature.value) offset.value = 0
  const signature = filterSignature.value
  const current = ++revision
  loading.value = true
  error.value = ''
  try {
    appliedFilter.value = signature
    const params = usageParams()
    params.set('limit', '100')
    params.set('offset', String(offset.value))
    const [keyResult, usageResult, resourceResult] = await Promise.all([
      api<{ data: ApiKeyRecord[] }>('/api/api-keys'),
      api<UsageReport>(`/api/usage?${params.toString()}`),
      api<ResourceSnapshot>('/api/resource-policy'),
    ])
    if (current === revision) { keys.value = keyResult.data; report.value = usageResult; resources.value = resourceResult }
  } catch (value) {
    if (current === revision) error.value = describeError(value) + (report.value ? '；仍显示上次成功查询的结果。' : '')
  } finally {
    if (current === revision) loading.value = false
  }
}

async function createKey() {
  if (!createName.value.trim() || creating.value || mutatingId.value) return
  creating.value = true
  error.value = ''
  try {
    createdKey.value = await api<CreatedKey>('/api/api-keys', {
      method: 'POST',
      body: JSON.stringify({ name: createName.value.trim(), policy: normalizePolicy(createPolicy.value) }),
    })
    createName.value = ''
    createPolicy.value = defaultKeyPolicy()
    createOpen.value = false
    copied.value = false
    await load()
  } catch (value) {
    error.value = describeError(value)
  } finally {
    creating.value = false
  }
}

async function setEnabled(item: ApiKeyRecord, enabled: boolean) {
  if (mutatingId.value || creating.value) return
  if (!enabled && !window.confirm(`停用 ${item.name}？该 Key 的后续新请求将返回 401，可再次启用。`)) return
  mutatingId.value = item.id
  error.value = ''
  try {
    await api(`/api/api-keys/${item.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ enabled }),
    })
    message.value = `${item.name} 已${enabled ? '启用' : '停用'}。`
    await load()
  } catch (value) {
    error.value = describeError(value)
  } finally {
    mutatingId.value = ''
  }
}

async function revokeKey() {
  if (!revokeTarget.value || mutatingId.value || creating.value) return
  const item = revokeTarget.value
  mutatingId.value = item.id
  error.value = ''
  try {
    await api(`/api/api-keys/${item.id}`, { method: 'DELETE' })
    message.value = `${item.name} 已撤销，历史用量仍可按该 Key 查询。`
    revokeTarget.value = null
    await load()
  } catch (value) {
    error.value = describeError(value)
  } finally {
    mutatingId.value = ''
  }
}

async function copyKey() {
  if (!createdKey.value) return
  try {
    await navigator.clipboard.writeText(createdKey.value.key)
    copied.value = true
    window.setTimeout(() => { copied.value = false }, 1800)
  } catch { error.value = '当前浏览器不允许自动复制，请手动选择并复制下方完整 Key。' }
}

async function renameKey() {
  if (!renameTarget.value || !renameName.value.trim() || mutatingId.value) return
  mutatingId.value = renameTarget.value.id
  error.value = ''
  try {
    await api(`/api/api-keys/${mutatingId.value}`, { method: 'PATCH', body: JSON.stringify({ name: renameName.value.trim() }) })
    message.value = 'Key 名称已更新，不影响密钥和历史用量。'
    renameTarget.value = null
    await load()
  } catch (reason) { error.value = describeError(reason) } finally { mutatingId.value = '' }
}
function page(delta: number) { offset.value = Math.max(0, offset.value + delta * 100); load(false) }

function openPolicy(item: ApiKeyRecord) {
  policyTarget.value = item
  editPolicy.value = JSON.parse(JSON.stringify(item.policy || defaultKeyPolicy()))
  error.value = ''
}

async function savePolicy() {
  if (!policyTarget.value || mutatingId.value || creating.value) return
  const item = policyTarget.value
  mutatingId.value = item.id; error.value = ''
  try {
    await api(`/api/api-keys/${item.id}`, { method: 'PATCH', body: JSON.stringify({ policy: normalizePolicy(editPolicy.value) }) })
    message.value = `${item.name} 的权限与限制已保存，对后续新请求生效。`
    policyTarget.value = null
    await load()
  } catch (reason) { error.value = describeError(reason) } finally { mutatingId.value = '' }
}

function clearDates() {
  startDate.value = ''
  endDate.value = ''
  load()
}

onMounted(async () => {
  await load()
  if (requestFilter.value || route.hash === '#request-details') {
    await nextTick()
    showRequests()
  }
})
</script>

<template>
  <div class="page-body usage-page">
    <section class="usage-hero panel">
      <div>
        <span class="hero-icon"><KeyRound :size="20" /></span>
        <div><h2>Key 权限、资源与用量</h2><p>配置模型授权、有效期和调用限额，查看在途请求及用量预警。</p></div>
      </div>
      <div class="key-list-actions"><button class="secondary-button" @click="showRequests">查看调用明细</button><button class="primary-button" :disabled="creating || !!mutatingId || !resources" @click="error = ''; createPolicy = defaultKeyPolicy(); createOpen = true"><Plus :size="15" /> 新建 API Key</button></div>
    </section>

    <div v-if="error" class="notice error-notice" role="alert"><Ban :size="15" />{{ error }}</div>
    <div v-if="message" class="notice success-notice" role="status">{{ message }}</div>
    <ActiveRequests @snapshot="value => { if (resources) { resources.in_flight = value.in_flight; resources.policy = value.policy } }" />

    <section class="metric-grid usage-metrics">
      <article class="metric-card compact-metric"><small>有效 API Key</small><strong>{{ activeKeys }}</strong><p>共 {{ keys.length }} 个历史 Key</p></article>
      <article class="metric-card compact-metric"><small>已知输入 Token 小计</small><strong>{{ formatNumber(report?.summary.input_tokens) }}</strong><p>缺失计数：{{ formatNumber(report?.summary.missing_input_tokens_requests) }} 次</p></article>
      <article class="metric-card compact-metric"><small>已知输出 Token 小计</small><strong>{{ formatNumber(report?.summary.output_tokens) }}</strong><p>缺失计数：{{ formatNumber(report?.summary.missing_output_tokens_requests) }} 次</p></article>
      <article class="metric-card compact-metric accent-total"><small>已知总 Token 小计</small><strong>{{ formatNumber(report?.summary.total_tokens) }}</strong><p>{{ formatNumber(report?.summary.request_count) }} 条计量记录（含失败/拒绝）</p></article>
    </section>

    <ResourceControls :snapshot="resources" @refresh="load(false)" />

    <section class="panel key-panel">
      <div class="panel-head">
        <div><h2>Key 管理</h2><p>Key 明文仅在创建成功时展示一次，服务端只保存不可逆摘要。</p></div>
        <div class="key-list-actions"><label><input v-model="showRevoked" type="checkbox" />显示已撤销（{{ revokedCount }}）</label><button class="secondary-button" :disabled="loading" @click="load()"><RefreshCw :class="{ spin: loading }" :size="14" />刷新</button></div>
      </div>
      <div class="table-wrap">
        <table class="management-table key-table">
          <thead><tr><th>名称 / 前缀</th><th>状态 / 到期</th><th>模型权限</th><th>限额 / 在途</th><th>最近使用</th><th>计量记录数</th><th>历史已知 Token 小计</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-if="!visibleKeys.length"><td colspan="8" class="empty-row">暂无未撤销的 Key，可勾选“显示已撤销”查看历史。</td></tr>
            <tr v-for="item in visibleKeys" :key="item.id">
              <td><strong>{{ item.name }}</strong><code>{{ item.prefix }}••••••••</code><small class="key-detail">{{ item.policy?.application || '未设置归属' }}</small></td>
              <td><span :class="['status-pill', (!item.enabled || item.revoked_at || item.access?.expired) && 'status-inactive']"><i></i>{{ item.revoked_at ? '已撤销' : !item.enabled ? '已停用' : item.access?.expired ? '已到期' : '启用' }}</span><small class="key-detail">{{ item.policy?.expires_at ? new Date(item.policy.expires_at).toLocaleString() : '永久有效' }}</small></td>
              <td><strong>{{ item.policy?.allowed_models == null ? '全部模型' : item.policy.allowed_models.length ? `已授权 ${item.policy.allowed_models.length} 个` : '禁止全部模型' }}</strong><details v-if="item.policy?.allowed_models?.length" class="key-model-list"><summary>查看授权</summary><code v-for="id in item.policy.allowed_models" :key="id">{{ id }}</code></details></td>
              <td class="key-limits"><span>频率 {{ item.policy?.rate_limit_per_minute || resources?.default_rpm }} / 分钟</span><span>在途 {{ resources?.in_flight.key[item.id] || 0 }} / {{ item.policy?.max_concurrent_requests || '不限' }}</span><span>今日受理 {{ item.access?.daily_requests || 0 }} / {{ item.policy?.daily_request_limit || '不限' }}</span><small v-if="item.access?.daily_remaining === 0" class="quota-warning">今日额度已用完</small><small v-if="item.access?.token_warning_reached" class="quota-warning">本月已知 Token 达到预警线</small><details v-if="item.policy?.monthly_token_warning"><summary>月度预警详情</summary><small>已知 {{ formatNumber(item.access?.monthly_known_tokens) }} / {{ formatNumber(item.policy.monthly_token_warning) }} Token；{{ item.access?.monthly_uncertain_requests || 0 }} 条计数不确定。仅预警，不拦截。</small></details></td>
              <td>{{ formatDate(item.last_used_at) }}</td>
              <td>{{ formatNumber(item.request_count) }}</td>
              <td class="token-cell"><strong>{{ formatNumber(item.total_tokens) }}</strong><small>入 {{ formatNumber(item.input_tokens) }} · 出 {{ formatNumber(item.output_tokens) }}</small></td>
              <td><div class="row-actions" v-if="!item.revoked_at">
                <button class="tiny-button" :disabled="!!mutatingId || creating" @click="renameTarget = item; renameName = item.name; error = ''">改名</button>
                <button class="tiny-button" :disabled="!!mutatingId || creating || !resources" @click="openPolicy(item)">权限与限制</button>
                <button class="tiny-button" :disabled="!!mutatingId || creating" @click="setEnabled(item, !item.enabled)">{{ item.enabled ? '停用' : '启用' }}</button>
                <button class="tiny-button danger-text" :disabled="!!mutatingId || creating" @click="revokeTarget = item; error = ''"><Trash2 :size="12" />撤销</button>
              </div><span v-else class="muted">不可恢复</span></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="panel usage-query">
      <div class="panel-head"><div><h2>用量查询与汇总</h2><p>日期按本机时区选择；展示已知计数小计，不是完整账单。管理台接口测试不计入业务 Key。</p></div></div>
      <div class="usage-filters">
        <label><span>API Key</span><select v-model="selectedKey"><option value="">全部 Key</option><option v-for="item in keys" :key="item.id" :value="item.id">{{ item.name }} · {{ item.prefix }}</option></select></label>
        <label><span>开始日期</span><input v-model="startDate" type="date" /></label>
        <label><span>结束日期</span><input v-model="endDate" type="date" /></label>
        <label><span>模型 ID（精确）</span><input v-model="modelFilter" maxlength="128" placeholder="全部模型" /></label>
        <label><span>Request ID（精确）</span><input v-model="requestFilter" maxlength="128" placeholder="查询指定请求" /></label>
        <label><span>请求状态</span><select v-model="statusFilter"><option value="">全部状态</option><option v-for="value in ['succeeded','rejected','failed','cancelled','interrupted','upstream_error','stream_error','invalid_response']" :key="value" :value="value">{{ requestStatus(value) }}</option></select></label>
        <label><span>具体原因</span><select v-model="reasonFilter"><option value="">全部原因</option><option v-for="item in report?.reason_options" :key="item.code" :value="item.code">{{ item.label }}</option></select></label>
        <button class="primary-button" :disabled="loading" @click="load()"><Search :size="14" />查询</button>
        <button class="secondary-button" :disabled="loading" @click="clearDates">全部时间</button>
        <button class="secondary-button" :disabled="exporting || loading" @click="exportUsage">{{ exporting ? '正在导出…' : '导出全部匹配记录' }}</button>
      </div>
      <div class="usage-warning" v-if="report?.summary.unreported_requests || report?.summary.missing_total_tokens_requests">{{ report?.summary.unreported_requests || 0 }} 次没有有效用量报告，其中 {{ report?.summary.admission_rejected_requests || 0 }} 次为准入拒绝、无推理用量。其他未报告的计数仍为未知，不能当作 0；仅累加已知计数。</div>
      <div class="usage-warning" v-if="report?.summary.anomaly_requests">{{ report.summary.anomaly_requests }} 次计数不完整、无效或不一致；保留上游原值，请展开明细核对。</div>
      <div class="usage-warning" v-if="report?.summary.legacy_requests">{{ report.summary.legacy_requests }} 条历史记录缺少原始 usage，沿用原存储计数并标注历史口径，无法追溯重算。</div>
    </section>

    <div class="usage-grid">
      <section class="panel">
        <div class="panel-head"><div><h2>按 Key 汇总</h2><p>用于客户、业务系统或项目维度的成本核对。</p></div></div>
        <div class="table-wrap"><table><thead><tr><th>API Key</th><th>请求数</th><th>输入</th><th>输出</th><th>合计</th></tr></thead><tbody>
          <tr v-if="!report?.by_key.length"><td colspan="5" class="empty-row">当前范围暂无用量</td></tr>
          <tr v-for="item in report?.by_key" :key="item.api_key_id"><td><strong>{{ item.name }}</strong><code>{{ item.prefix }}</code></td><td>{{ formatNumber(item.request_count) }}</td><td>{{ formatNumber(item.input_tokens) }}</td><td>{{ formatNumber(item.output_tokens) }}</td><td><strong>{{ formatNumber(item.total_tokens) }}</strong><small v-if="item.missing_total_tokens_requests">另有 {{ item.missing_total_tokens_requests }} 次未知</small></td></tr>
        </tbody></table></div>
      </section>
      <section class="panel">
        <div class="panel-head"><div><h2>按模型汇总</h2><p>快速识别 Token 消耗主要来自哪些模型。</p></div></div>
        <div class="table-wrap"><table><thead><tr><th>模型</th><th>请求数</th><th>输入</th><th>输出</th><th>合计</th></tr></thead><tbody>
          <tr v-if="!report?.by_model.length"><td colspan="5" class="empty-row">当前范围暂无用量</td></tr>
          <tr v-for="item in report?.by_model" :key="item.model"><td><code>{{ item.model }}</code></td><td>{{ formatNumber(item.request_count) }}</td><td>{{ formatNumber(item.input_tokens) }}</td><td>{{ formatNumber(item.output_tokens) }}</td><td><strong>{{ formatNumber(item.total_tokens) }}</strong><small v-if="item.missing_total_tokens_requests">另有 {{ item.missing_total_tokens_requests }} 次未知</small></td></tr>
        </tbody></table></div>
      </section>
    </div>

    <section id="request-details" ref="requestDetails" class="panel">
      <div class="panel-head"><div><h2>调用明细</h2><p>每页 100 条。点击查看请求正文、参数、来源、耗时和结果。历史未保存的正文无法补回；时间按浏览器时区显示。</p></div><button class="secondary-button" :disabled="loading" @click="load(false)">刷新调用明细</button></div>
      <UsageRequests :events="report?.recent || []" />
      <div class="pagination"><button class="secondary-button" :disabled="loading || !report?.pagination?.offset" @click="page(-1)">上一页</button><span>共 {{ report?.pagination?.total || 0 }} 条 · 第 {{ Math.floor((report?.pagination?.offset || 0) / 100) + 1 }} 页</span><button class="secondary-button" :disabled="loading || !report?.pagination?.has_more" @click="page(1)">下一页</button></div>
    </section>

    <div v-if="createOpen" class="modal-backdrop" @click.self="!creating && (createOpen = false)">
      <form class="modal-card key-policy-dialog" role="dialog" aria-modal="true" aria-label="新建 API Key" @submit.prevent="createKey">
        <p v-if="error" role="alert">{{ error }}</p>
        <button type="button" class="modal-close" :disabled="creating" @click="createOpen = false"><X :size="15" /></button>
        <span class="modal-symbol"><KeyRound :size="20" /></span><h2>新建 API Key</h2><p>建议按客户、系统或项目命名，方便后续用量核对。</p>
        <label class="modal-field"><span>Key 名称</span><input v-model="createName" maxlength="64" autofocus placeholder="例如：客户 A · 生产环境" /></label>
        <KeyPolicyFields v-model="createPolicy" :models="resources?.models || []" :default-rpm="resources?.default_rpm || 120" :disabled="creating" />
        <div class="modal-actions"><button type="button" class="secondary-button" :disabled="creating" @click="createOpen = false">取消</button><button class="primary-button" :disabled="creating || !createName.trim()"><LoaderCircle v-if="creating" class="spin" :size="14" /><Plus v-else :size="14" />创建</button></div>
      </form>
    </div>

    <div v-if="policyTarget" class="modal-backdrop" @click.self="!mutatingId && (policyTarget = null)"><form class="modal-card key-policy-dialog" role="dialog" aria-modal="true" aria-label="Key 权限与限制" @submit.prevent="savePolicy"><h2>{{ policyTarget.name }} · 权限与限制</h2><p>保存后对新请求生效，不中断在途请求。空白名单禁止全部模型；修改额度不清空当日已受理次数。</p><p v-if="error" role="alert" class="policy-error">{{ error }}</p><KeyPolicyFields v-model="editPolicy" :models="resources?.models || []" :default-rpm="resources?.default_rpm || 120" :disabled="!!mutatingId" /><div class="modal-actions"><button type="button" class="secondary-button" :disabled="!!mutatingId" @click="policyTarget = null">取消</button><button class="primary-button" :disabled="!!mutatingId">{{ mutatingId ? '正在保存…' : '保存权限与限制' }}</button></div></form></div>

    <div v-if="createdKey" class="modal-backdrop">
      <div class="modal-card key-created-card" role="dialog" aria-modal="true" aria-label="API Key 已创建"><span class="modal-symbol"><ShieldCheck :size="20" /></span><h2>API Key 已创建</h2><p>请立即复制并交给对应调用方。关闭窗口后无法再次查看完整 Key。</p><p v-if="error" role="alert">{{ error }}</p>
        <div class="secret-box"><code>{{ createdKey.key }}</code><button class="secondary-button" @click="copyKey"><Check v-if="copied" :size="14" /><Clipboard v-else :size="14" />{{ copied ? '已复制' : '复制' }}</button></div>
        <div class="modal-actions"><button class="primary-button" @click="createdKey = null">我已安全保存</button></div>
      </div>
    </div>

    <div v-if="renameTarget" class="modal-backdrop">
      <form class="modal-card" role="dialog" aria-modal="true" aria-label="修改 Key 名称" @submit.prevent="renameKey"><h2>修改 Key 名称</h2><p>只修改名称，不更换密钥。</p><p v-if="error" role="alert">{{ error }}</p><label class="modal-field">名称<input v-model="renameName" maxlength="64" /></label><div class="modal-actions"><button type="button" class="secondary-button" :disabled="!!mutatingId" @click="renameTarget = null">取消</button><button class="primary-button" :disabled="!!mutatingId || !renameName.trim()">保存名称</button></div></form>
    </div>

    <div v-if="revokeTarget" class="modal-backdrop" @click.self="revokeTarget = null">
      <div class="modal-card"><button class="modal-close" @click="revokeTarget = null"><X :size="15" /></button><span class="modal-symbol revoke-symbol"><Trash2 :size="20" /></span><h2>撤销 {{ revokeTarget.name }}？</h2><p>撤销后该 Key 立即无法调用模型，且不能恢复；历史 Token 统计仍会保留。</p><div class="impact-box"><strong>影响范围</strong><span>前缀 {{ revokeTarget.prefix }} 的所有新请求将返回 401。</span></div><div class="modal-actions"><button class="secondary-button" @click="revokeTarget = null">取消</button><button class="danger-button" :disabled="mutatingId === revokeTarget.id" @click="revokeKey"><Trash2 :size="14" />确认撤销</button></div></div>
    </div>
  </div>
</template>

<style scoped>
.usage-page { --usage-blue: #2d6f86; }
.usage-hero { padding: 18px; display: flex; align-items: center; justify-content: space-between; gap: 18px; }
.usage-hero > div { display: flex; align-items: center; gap: 12px; }
.usage-hero p { margin-top: 4px; color: #879399; font-size: 10px; }
.hero-icon { width: 40px; height: 40px; display: grid; place-items: center; color: #176d56; background: #e2f4ed; border-radius: 10px; }
.usage-metrics { margin: 0; }
.compact-metric { min-height: 118px; padding: 16px 17px; }
.compact-metric > small { display: block; color: #879399; font-size: 9px; }
.compact-metric > strong { margin-top: 10px; font-size: 23px; }
.compact-metric > p { margin-top: 4px; color: #929da2; font-size: 9px; }
.accent-total { border-top: 3px solid var(--signal); padding-top: 14px; }
.key-panel { overflow: hidden; }
.key-list-actions { display:flex; flex-wrap:wrap; align-items:center; gap:12px; }.key-list-actions label { display:flex; align-items:center; gap:6px; font-size:11px; color:#61777e; }
.key-table code, .usage-grid code { margin-top: 3px; color: #8b989d; font-size: 8px; }
.token-cell strong, .token-cell small { display: block; }.token-cell small { margin-top: 3px; color: #97a2a7; font-size: 8px; }
.row-actions { display: flex; align-items: center; gap: 6px; }
.key-policy-dialog { width:min(760px,calc(100vw - 32px)); max-height:90vh; overflow:auto; text-align:left; }.key-policy-dialog > p { font-size:12px; line-height:1.6; color:#71868b; }.key-policy-dialog .policy-error { color:#a14343; }
.key-detail { display:block; margin-top:6px; color:#71868b; font-size:11px; }.key-limits { min-width:165px; }.key-limits > span { display:block; font-size:11px; line-height:1.8; }.key-limits details { font-size:11px; max-width:190px; line-height:1.6; }.quota-warning { display:block; color:#a66b14; font-size:11px; margin-top:5px; }.key-model-list { font-size:11px; max-width:190px; }.key-model-list code { display:block; overflow-wrap:anywhere; }.row-actions { flex-wrap:wrap; min-width:155px; }
.tiny-button { height: 28px; padding: 0 8px; display: inline-flex; align-items: center; gap: 4px; color: #52656c; border: 1px solid #d8e0e1; background: #fff; border-radius: 6px; font-size: 9px; }
.danger-text { color: #a34747; }
.usage-filters { padding: 14px 18px 17px; display: grid; grid-template-columns: repeat(auto-fit,minmax(150px,1fr)); align-items: end; gap: 9px; }
.usage-filters label span, .modal-field span { display: block; margin-bottom: 6px; color: #6f7d83; font-size: 9px; font-weight: 700; }
.usage-filters select, .usage-filters input, .modal-field input { width: 100%; height: 36px; padding: 0 10px; color: #405159; border: 1px solid #d6dfe0; background: #fff; border-radius: 7px; outline: none; font-size: 10px; }
.usage-warning { margin: 0 18px 16px; padding: 9px 11px; color: #816220; background: #fff8e9; border: 1px solid #ead9ad; border-radius: 7px; font-size: 9px; }
.usage-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 15px; }
.usage-grid .panel { overflow: hidden; }
.modal-field { display: block; margin: 18px 0; }
.secret-box { margin: 18px 0; padding: 12px; display: flex; align-items: center; gap: 10px; border: 1px solid #bfded2; background: #f1faf6; border-radius: 9px; }
.secret-box code { min-width: 0; flex: 1; overflow-wrap: anywhere; color: #174f40; font-size: 10px; line-height: 1.6; }
.secret-box button { flex: none; }
.revoke-symbol { color: #a14343; background: #fbe9e7; }
.pagination { display:flex; align-items:center; justify-content:flex-end; flex-wrap:wrap; gap:12px; padding:14px; font-size:12px; }
.usage-grid small { display:block; color:#966d21; }
@media (max-width: 1050px) { .usage-filters { grid-template-columns: 1fr 1fr 1fr; }.usage-filters button { width: 100%; }.usage-grid { grid-template-columns: 1fr; } }
@media (max-width: 760px) { .usage-hero { align-items: flex-start; flex-direction: column; }.usage-hero > button { width: 100%; }.usage-filters { grid-template-columns: 1fr 1fr; }.usage-filters label:first-child { grid-column: 1 / -1; } }
@media (max-width: 480px) { .usage-filters { grid-template-columns: 1fr; }.usage-filters label:first-child { grid-column: auto; }.secret-box { align-items: stretch; flex-direction: column; } }
</style>
