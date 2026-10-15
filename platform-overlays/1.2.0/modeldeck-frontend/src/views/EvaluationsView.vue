<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { ClipboardCheck, Download, LoaderCircle, RefreshCw, Search, TriangleAlert } from '@lucide/vue'
import { api, ApiError, describeError } from '../api'
import { operationState } from '../operations'
import {
  caseStatusLabels, evaluationSelectionError, evaluationTime, evaluationVerdict, evidenceText,
  isActiveRun, latencyText, modelUnavailableReason, newEvaluationId, runStatusLabels, suiteLabels, verdictLabels,
  type CaseStatus, type EvaluationCatalog, type EvaluationListing, type EvaluationPlan, type EvaluationRun, type EvaluationSuite,
} from '../evaluations'

const pendingKey = 'modeldeck.evaluations.pending-id'
const selectedKey = 'modeldeck.evaluations.selected-id'
const catalog = ref<EvaluationCatalog | null>(null)
const catalogLoading = ref(false), catalogError = ref(''), catalogAt = ref('')
const runs = ref<EvaluationRun[]>([]), historyLoading = ref(false), historyLoaded = ref(false), historyError = ref('')
const search = ref(''), device = ref(''), selectedModels = ref<string[]>([])
const suite = ref<EvaluationSuite>('smoke'), repeats = ref(1)
const activeId = ref(''), pendingId = ref(''), pendingUnknown = ref(false), selectedId = ref('')
const detail = ref<EvaluationRun | null>(null), detailLoading = ref(false), detailError = ref(''), detailAt = ref('')
const pollError = ref(''), storageError = ref(''), actionError = ref(''), notice = ref('')
const submitting = ref(false), checking = ref(false), reconciling = ref(false), cancelBusy = ref(''), exporting = ref('')
const caseFilter = ref<CaseStatus | ''>(''), caseSearch = ref('')
const confirmation = ref<(EvaluationPlan & { version: string }) | null>(null), impactAccepted = ref(false)
const confirmDialog = ref<HTMLDialogElement | null>(null), startButton = ref<HTMLButtonElement | null>(null)
const detailPanel = ref<HTMLElement | null>(null)
const cancelUncertainIds = ref<string[]>([])
let stopped = false, mutationVersion = 0, polling = false
let pollTimer: ReturnType<typeof setTimeout> | undefined
const requests = new Set<AbortController>()
const runReadVersions = new Map<string, number>()

const plan = computed<EvaluationPlan>(() => ({ models: [...selectedModels.value], suite: suite.value, repeats: suite.value === 'repeat' ? repeats.value : 1 }))
const devices = computed(() => [...new Set(catalog.value?.models.map(model => model.device) || [])].sort())
const visibleModels = computed(() => (catalog.value?.models || []).filter(model => (!device.value || model.device === device.value)
  && `${model.id} ${model.device} ${model.capability}`.toLowerCase().includes(search.value.trim().toLowerCase())))
const availableModels = computed(() => visibleModels.value.filter(model => !modelUnavailableReason(model, suite.value)))
const maxModels = computed(() => Math.min(23, catalog.value?.limits.max_models ?? 23))
const maxRepeats = computed(() => Math.max(0, Math.min(3, catalog.value?.limits.max_repeats ?? 3)))
const formLocked = computed(() => submitting.value || checking.value || !!confirmation.value)
const selectionError = computed(() => evaluationSelectionError(catalog.value, plan.value))
const startBlocked = computed(() => {
  if (storageError.value) return storageError.value
  if (operationState.busy) return `管理操作尚未结束（${operationState.label || '状态待确认'}），请等待完成后开始评测。`
  if (pendingUnknown.value) return '上次提交结果尚未确认，请先查询已保存的请求 ID。'
  if (activeId.value || pendingId.value) return '已有评测未结束，请等待完成或取消后再开始。'
  if (catalogLoading.value || historyLoading.value || checking.value) return '正在核对可用模型与运行状态…'
  if (historyError.value || !historyLoaded.value) return '请先成功刷新运行记录，核对当前任务。'
  return selectionError.value
})
const activeRun = computed(() => runs.value.find(run => run.id === activeId.value))
const visibleCases = computed(() => (detail.value?.cases || []).filter(item => (!caseFilter.value || item.status === caseFilter.value)
  && `${item.model} ${item.name} ${item.reason || ''}`.toLowerCase().includes(caseSearch.value.trim().toLowerCase())))
const finishedCases = computed(() => detail.value?.cases.filter(item => !['pending', 'running'].includes(item.status)).length || 0)
const runningCases = computed(() => detail.value?.cases.filter(item => item.status === 'running').length || 0)
const interruptedCases = computed(() => detail.value?.cases.filter(item => item.status === 'interrupted').length || 0)
const detailVerdict = computed(() => detail.value ? evaluationVerdict(detail.value) : 'pending')
const latencyStats = computed(() => {
  const value = detail.value?.summary.latency_ms
  return [
    { label: '平均', value: value?.mean }, { label: 'P50', value: value?.p50 }, { label: 'P95', value: value?.p95 },
    { label: '最短', value: value?.min }, { label: '最长', value: value?.max },
  ].map(item => ({ label: item.label, text: latencyText(value?.count ? item.value : null) }))
})

watch(suite, () => { repeats.value = 1 })

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const controller = new AbortController()
  requests.add(controller)
  // These endpoints acknowledge jobs, rather than waiting for model inference.
  const timeout = setTimeout(() => controller.abort(), 20000)
  try { return await api<T>(path, { ...init, signal: controller.signal }) }
  finally { clearTimeout(timeout); requests.delete(controller) }
}

function saveSelection(id: string) {
  selectedId.value = id
  try { sessionStorage.setItem(selectedKey, id) } catch { /* pending ID is stored separately and is mandatory */ }
}

function clearPending(id: string) {
  if (pendingId.value !== id) return
  try {
    if (sessionStorage.getItem(pendingKey) === id) sessionStorage.removeItem(pendingKey)
    pendingId.value = ''; pendingUnknown.value = false
  } catch { storageError.value = '浏览器无法更新评测恢复记录，请恢复会话存储后刷新页面。' }
}

function acceptRun(run: EvaluationRun) {
  const wasActive = activeId.value === run.id
  const cancellationConfirmed = cancelUncertainIds.value.includes(run.id) && (run.status === 'cancelling' || !isActiveRun(run))
  runs.value = [run, ...runs.value.filter(item => item.id !== run.id)]
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()).slice(0, 30)
  if (selectedId.value === run.id) { detail.value = run; detailAt.value = new Date().toLocaleTimeString('zh-CN'); detailError.value = '' }
  if (pendingId.value === run.id) {
    if (pendingUnknown.value) actionError.value = ''
    pendingUnknown.value = false
  }
  if (isActiveRun(run)) activeId.value = run.id
  else {
    if (activeId.value === run.id) activeId.value = ''
    clearPending(run.id)
    cancelUncertainIds.value = cancelUncertainIds.value.filter(id => id !== run.id)
  }
  if (run.status === 'cancelling') cancelUncertainIds.value = cancelUncertainIds.value.filter(id => id !== run.id)
  if (wasActive || activeId.value === run.id) pollError.value = ''
  if (cancellationConfirmed && actionError.value.startsWith('取消结果未确认')) {
    actionError.value = ''
    notice.value = run.status === 'cancelling' ? '已查询确认取消正在处理，等待当前请求结束，最长可能需要约 180 秒。' : '已查询到任务最终状态，请查看保留的结果。'
  }
}

async function loadCatalog(): Promise<boolean> {
  if (catalogLoading.value || stopped) return false
  catalogLoading.value = true; catalogError.value = ''; catalog.value = null
  try {
    const value = await request<EvaluationCatalog>('/api/evaluations/catalog')
    if (stopped) return false
    catalog.value = value; catalogAt.value = new Date().toLocaleTimeString('zh-CN')
    if (value.active_run_id) activeId.value = value.active_run_id
    return true
  } catch (error) {
    if (!stopped) catalogError.value = `模型目录读取失败：${describeError(error)}。刷新成功前无法启动评测。`
    return false
  } finally { catalogLoading.value = false }
}

async function loadHistory(): Promise<boolean> {
  if (historyLoading.value || stopped) return false
  historyLoading.value = true; historyError.value = ''
  const revision = mutationVersion
  try {
    const value = await request<EvaluationListing>('/api/evaluations?limit=30')
    if (stopped || revision !== mutationVersion) return false
    runs.value = value.runs; historyLoaded.value = true
    if (value.active_run_id) activeId.value = value.active_run_id
    else if (!activeId.value) activeId.value = value.runs.find(isActiveRun)?.id || ''
    return true
  } catch (error) {
    if (!stopped) historyError.value = `运行记录刷新失败：${describeError(error)}。下方保留的历史数据不能代表当前状态。`
    return false
  } finally { historyLoading.value = false }
}

async function loadRun(id: string, background = false): Promise<boolean> {
  if (!id || stopped) return false
  const revision = mutationVersion
  const readVersion = (runReadVersions.get(id) || 0) + 1
  runReadVersions.set(id, readVersion)
  if (!background && selectedId.value === id) { detailLoading.value = true; detailError.value = '' }
  try {
    const run = await request<EvaluationRun>(`/api/evaluations/${encodeURIComponent(id)}`)
    if (stopped || revision !== mutationVersion || runReadVersions.get(id) !== readVersion) return false
    if (run.id !== id) throw new Error('返回的评测 ID 不一致')
    acceptRun(run)
    return true
  } catch (error) {
    if (stopped || revision !== mutationVersion || runReadVersions.get(id) !== readVersion) return false
    const message = error instanceof ApiError && error.status === 404 && pendingId.value === id
      ? '暂未查到该请求。提交可能尚未落盘；已保留请求 ID，请稍后再次查询，不会重新提交。'
      : `状态读取失败：${describeError(error)}。显示内容以最后成功读取为准，不代表任务已停止。`
    if (selectedId.value === id) detailError.value = message
    if (background || activeId.value === id) pollError.value = message
    if (pendingId.value === id && pendingUnknown.value) actionError.value = message
    return false
  } finally {
    if (selectedId.value === id && runReadVersions.get(id) === readVersion) detailLoading.value = false
  }
}

function schedulePoll() {
  clearTimeout(pollTimer)
  if (stopped || !activeId.value || polling || submitting.value || cancelBusy.value) return
  pollTimer = setTimeout(async () => {
    if (stopped) return
    polling = true
    try { if (!document.hidden && activeId.value) await loadRun(activeId.value, true) }
    finally { polling = false; schedulePoll() }
  }, 3000)
}

async function revealDetail() {
  await nextTick()
  if (stopped || !detailPanel.value) return
  detailPanel.value.focus({ preventScroll: true })
  detailPanel.value.scrollIntoView({ block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
}

async function selectRun(id: string, reveal = false) {
  if (selectedId.value !== id) {
    detail.value = null; detailAt.value = ''; caseFilter.value = ''; caseSearch.value = ''
  }
  saveSelection(id)
  await loadRun(id)
  schedulePoll()
  if (reveal && selectedId.value === id) await revealDetail()
}

async function refreshSources() {
  const [catalogOK, historyOK] = await Promise.all([loadCatalog(), loadHistory()])
  return catalogOK && historyOK
}

async function refreshAll() {
  if (catalogLoading.value || historyLoading.value || submitting.value || checking.value) return
  await refreshSources()
  if (stopped) return
  const id = selectedId.value || pendingId.value || activeId.value || runs.value[0]?.id
  if (id) await selectRun(id)
  if (activeId.value && activeId.value !== id) await loadRun(activeId.value, true)
  schedulePoll()
}

async function reconcile() {
  if (!pendingId.value || reconciling.value || submitting.value) return
  reconciling.value = true; actionError.value = ''
  try { await selectRun(pendingId.value) }
  finally { reconciling.value = false }
}

function selectAvailable() {
  const ids = new Set(selectedModels.value)
  for (const model of availableModels.value) { if (ids.size >= maxModels.value) break; ids.add(model.id) }
  selectedModels.value = [...ids]
}

async function reviewPlan() {
  if (formLocked.value || startBlocked.value) return
  checking.value = true; actionError.value = ''; notice.value = ''
  const candidate = plan.value
  try {
    if (!await refreshSources() || stopped) return
    const problem = evaluationSelectionError(catalog.value, candidate)
    if (problem || activeId.value || pendingId.value || operationState.busy) { actionError.value = problem || '已有评测或管理操作未结束，请先查看当前任务。'; return }
    confirmation.value = { ...candidate, version: catalog.value!.version }; impactAccepted.value = false
    await nextTick()
    confirmDialog.value?.showModal()
  } finally { checking.value = false; schedulePoll() }
}

function closeConfirmation() {
  if (submitting.value) return
  confirmDialog.value?.close(); confirmation.value = null; impactAccepted.value = false
  nextTick(() => startButton.value?.focus())
}

async function startEvaluation() {
  if (submitting.value || !confirmation.value || !impactAccepted.value || pendingId.value || activeId.value || storageError.value || operationState.busy) return
  const candidate = confirmation.value
  submitting.value = true; actionError.value = ''; notice.value = ''; clearTimeout(pollTimer)
  let id = ''
  let posted = false
  try {
    if (!await loadCatalog() || stopped) return
    const problem = evaluationSelectionError(catalog.value, candidate)
    if (problem || activeId.value || operationState.busy || catalog.value?.version !== candidate.version) {
      actionError.value = problem || (activeId.value || operationState.busy ? '已有评测或管理操作未结束，请先查看当前任务。' : '评测目录版本已变化，请返回重新确认。')
      return
    }
    id = newEvaluationId()
    // Do not send anything unless the idempotency ID can survive a page refresh.
    try {
      sessionStorage.setItem(pendingKey, id)
      if (sessionStorage.getItem(pendingKey) !== id) throw new Error('会话存储校验失败')
    } catch {
      storageError.value = '浏览器无法保存评测请求 ID，本次未提交。请启用会话存储后刷新页面。'
      return
    }
    pendingId.value = id; pendingUnknown.value = true; saveSelection(id)
    detail.value = null; detailAt.value = ''; detailError.value = ''; caseFilter.value = ''; caseSearch.value = ''
    mutationVersion++; posted = true
    const run = await request<EvaluationRun>('/api/evaluations', {
      method: 'POST', body: JSON.stringify({ id, models: candidate.models, suite: candidate.suite, repeats: candidate.repeats }),
    })
    if (stopped) return
    if (run.id !== id) throw new Error('返回的评测 ID 不一致，需按已保存的 ID 核对')
    acceptRun(run)
    notice.value = '评测已受理。离开页面后仍会执行，可从运行记录恢复查看。'
  } catch (error) {
    if (stopped) return
    // A timeout / lost response / server error may follow a committed job. Never repost.
    const rejected = error instanceof ApiError && [400, 401, 403, 404, 409, 413, 422, 429].includes(error.status)
    if (posted && !rejected) {
      actionError.value = `提交结果未确认：${describeError(error)}。请求 ID 已保留，正在查询，未重新提交。`
      if (await loadRun(id)) { actionError.value = ''; notice.value = '已按保存的请求 ID 找到评测，继续查看该任务。' }
    } else {
      if (posted) {
        clearPending(id)
        if (selectedId.value === id) saveSelection('')
      }
      actionError.value = `评测未受理：${describeError(error)}`
      await refreshSources()
    }
  } finally {
    submitting.value = false
    if (!stopped) {
      closeConfirmation(); schedulePoll()
      if (posted && selectedId.value === id) await revealDetail()
    }
  }
}

async function cancelRun(run: EvaluationRun) {
  if (!isActiveRun(run) || run.status === 'cancelling' || cancelBusy.value || cancelUncertainIds.value.includes(run.id)) return
  if (!window.confirm('停止后续评测用例？当前推理请求会继续到结束，最长可能需要约 180 秒，已完成的结果会保留。')) return
  cancelBusy.value = run.id; actionError.value = ''; notice.value = ''; mutationVersion++; clearTimeout(pollTimer)
  try {
    const value = await request<EvaluationRun>(`/api/evaluations/${encodeURIComponent(run.id)}/cancel`, { method: 'POST' })
    if (stopped) return
    if (value.id !== run.id) throw new Error('返回的评测 ID 不一致')
    acceptRun(value)
    notice.value = isActiveRun(value) ? '取消请求已受理，等待当前推理结束后停止后续用例，最长可能需要约 180 秒。' : '已读取任务最终状态，请查看保留的结果。'
  } catch (error) {
    if (stopped) return
    cancelUncertainIds.value = [...cancelUncertainIds.value, run.id]
    actionError.value = `取消结果未确认：${describeError(error)}。将继续查询状态；没有自动重发取消请求。`
    await loadRun(run.id)
  } finally { cancelBusy.value = ''; schedulePoll() }
}

async function exportRun(format: 'json' | 'md') {
  if (!detail.value || exporting.value) return
  const id = detail.value.id
  exporting.value = format; actionError.value = ''
  const controller = new AbortController(); requests.add(controller)
  const timeout = setTimeout(() => controller.abort(), 20000)
  try {
    const response = await fetch(`/api/evaluations/${encodeURIComponent(id)}/export?format=${format}`, { credentials: 'include', signal: controller.signal })
    if (!response.ok) {
      const payload = await response.json().catch(() => null)
      throw new ApiError(response.status, payload?.detail || '导出失败，请检查登录状态与服务连接')
    }
    if (response.redirected || (response.headers.get('Content-Type') || '').includes('text/html')) throw new Error('未取得导出文件，请检查登录状态')
    const blob = await response.blob()
    if (stopped) return
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url; anchor.download = `evaluation-${id}.${format}`
    document.body.appendChild(anchor); anchor.click(); anchor.remove()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch (error) { if (!stopped) actionError.value = `导出失败：${describeError(error)}` }
  finally { clearTimeout(timeout); requests.delete(controller); exporting.value = '' }
}

onMounted(async () => {
  try {
    const pending = sessionStorage.getItem(pendingKey)
    const selected = sessionStorage.getItem(selectedKey)
    if (pending && !/^[a-f0-9]{32}$/.test(pending)) storageError.value = '保存的评测请求 ID 无效，请核对浏览器会话存储后刷新页面。'
    else if (pending) { pendingId.value = pending; pendingUnknown.value = true }
    selectedId.value = pendingId.value || (selected && /^[a-f0-9]{32}$/.test(selected) ? selected : '')
  } catch { storageError.value = '浏览器会话存储不可用，无法安全恢复评测请求。请启用后刷新页面。' }
  await refreshAll()
})

onUnmounted(() => {
  stopped = true; clearTimeout(pollTimer)
  for (const controller of requests) controller.abort()
  requests.clear()
})
</script>

<template>
  <div class="evaluation-page">
    <header class="evaluation-intro">
      <div><h2>自检与评测</h2><p>从可用模型开始，以实际输出核对执行与质量，留存每次评测证据。</p></div>
      <button class="evaluation-button" :disabled="catalogLoading || historyLoading || submitting || checking" @click="refreshAll"><RefreshCw :size="15" :class="{ spin: catalogLoading || historyLoading }" aria-hidden="true" />刷新目录与记录</button>
    </header>

    <div v-if="storageError" class="evaluation-alert danger" role="alert">{{ storageError }}</div>
    <div v-if="actionError" class="evaluation-alert danger" role="alert">{{ actionError }}</div>
    <div v-if="notice" class="evaluation-alert" role="status">{{ notice }}</div>
    <div v-if="pendingUnknown" class="evaluation-alert warning" role="status">
      <div><strong>有一笔提交需要核对</strong><p>请求 ID：<code>{{ pendingId }}</code>。刷新或断网后只查询该任务，不会自动重发。</p></div>
      <button class="evaluation-button" :disabled="reconciling || submitting" @click="reconcile">{{ reconciling ? '正在查询…' : '查询已保存的请求' }}</button>
    </div>
    <div v-if="activeId" class="evaluation-alert active-run" role="status">
      <div><strong>{{ activeRun ? runStatusLabels[activeRun.status] : '有评测正在执行' }}</strong><p><code>{{ activeId }}</code> · 每 3 秒查询当前任务；离开页面不会停止推理。</p><p v-if="activeRun?.status === 'cancelling'">等待当前请求结束后停止后续用例，最长可能需要约 180 秒。</p></div>
      <button class="evaluation-button" :disabled="detailLoading" @click="selectRun(activeId, true)">查看当前评测</button>
    </div>
    <p v-if="pollError" class="evaluation-alert danger" role="alert">{{ pollError }}</p>

    <section class="panel evaluation-create" aria-labelledby="evaluation-create-title" :aria-busy="catalogLoading">
      <div class="evaluation-section-head"><div><h3 id="evaluation-create-title">新建评测</h3><p>选择模型与套件，确认资源影响后手动开始。</p></div><span v-if="catalog" class="evaluation-muted">目录 {{ catalog.version }} · 更新于 {{ catalogAt }}</span></div>
      <p v-if="catalogError" class="evaluation-alert danger" role="alert">{{ catalogError }}</p>
      <div v-if="!catalog" class="evaluation-empty"><LoaderCircle v-if="catalogLoading" :size="24" class="spin" aria-hidden="true" /><ClipboardCheck v-else :size="28" aria-hidden="true" /><p>{{ catalogLoading ? '正在读取模型可用性…' : '模型目录暂不可用，请刷新后继续。' }}</p></div>
      <template v-else>
        <fieldset class="evaluation-fieldset" :disabled="formLocked"><legend>1. 选择评测套件</legend>
          <div class="evaluation-suites">
            <label v-for="item in catalog.suites" :key="item.id" class="evaluation-suite" :class="{ chosen: suite === item.id }">
              <input v-model="suite" type="radio" name="evaluation-suite" :value="item.id" :aria-label="item.name" /><span><strong>{{ item.name }}</strong><small>{{ item.description }}</small><small>范围：{{ item.scope === 'execution' ? '执行自检' : item.scope === 'quality' ? '质量核对' : item.scope }}</small></span>
            </label>
          </div>
          <p v-if="!catalog.suites.length" class="evaluation-muted">当前没有可用评测套件。</p>
          <label v-if="suite === 'repeat'" class="evaluation-repeat">每个用例重复次数<select v-model.number="repeats" aria-label="每个用例重复次数"><option v-for="n in maxRepeats" :key="n" :value="n">{{ n }} 次</option></select><span>最多 3 次；重复会增加真实推理请求。</span></label>
        </fieldset>

        <fieldset class="evaluation-fieldset" :disabled="formLocked"><legend>2. 选择模型 <span class="evaluation-muted">已选 {{ selectedModels.length }} / {{ maxModels }}</span></legend>
          <div class="evaluation-filters">
            <label class="evaluation-search"><Search :size="16" aria-hidden="true" /><input v-model="search" type="search" aria-label="搜索模型、设备或能力" placeholder="搜索模型、设备或能力" /></label>
            <label class="evaluation-device">设备<select v-model="device" aria-label="筛选设备"><option value="">全部设备</option><option v-for="value in devices" :key="value" :value="value">{{ value }}</option></select></label>
            <button type="button" class="evaluation-button" :disabled="!availableModels.length || selectedModels.length >= maxModels" @click="selectAvailable">全选当前可用</button>
            <button type="button" class="evaluation-button" :disabled="!selectedModels.length" @click="selectedModels = []">清空选择</button>
          </div>
          <p class="evaluation-muted">仅可选择当前可用且支持该套件的模型。全选仅作用于当前筛选结果。</p>
          <div v-if="visibleModels.length" class="evaluation-models">
            <label v-for="model in visibleModels" :key="model.id" class="evaluation-model" :class="{ chosen: selectedModels.includes(model.id), unavailable: !!modelUnavailableReason(model, suite) }" :data-model-id="model.id">
              <input v-model="selectedModels" type="checkbox" :value="model.id" :aria-label="`选择模型 ${model.id}`" :disabled="!selectedModels.includes(model.id) && (!!modelUnavailableReason(model, suite) || selectedModels.length >= maxModels)" />
              <span><strong>{{ model.id }}</strong><small>{{ model.device }} · {{ model.capability }} · {{ model.status === 'online' ? '在线' : model.status === 'inactive' ? '未启用' : model.status === 'unavailable' ? '不可用' : model.status }}</small><small :class="{ 'evaluation-reason': !!modelUnavailableReason(model, suite) }">{{ modelUnavailableReason(model, suite) || '可用于当前套件' }}</small></span>
            </label>
          </div>
          <p v-else class="evaluation-empty">{{ catalog.models.length ? '没有匹配的模型，请调整搜索或设备筛选。' : '目录中暂无模型。' }}</p>
          <p v-if="selectedModels.length" class="evaluation-selected">已选（含筛选外）：{{ selectedModels.join('、') }}</p>
          <p v-if="selectedModels.length && selectionError" class="evaluation-reason" role="status">{{ selectionError }}</p>
        </fieldset>

        <div class="evaluation-impact"><TriangleAlert :size="20" aria-hidden="true" /><div><strong>会执行真实推理，占用设备与网关资源</strong><p>评测可能增加其他请求的等待时间。每次最多 {{ maxModels }} 个模型、{{ Math.min(120, catalog.limits.max_cases) }} 个用例；实际用例数由所选套件确定，提交时由服务端校验。质量结论仅覆盖本次样例。</p></div></div>
      </template>
      <div class="evaluation-start-row"><p id="evaluation-start-help" class="evaluation-muted">{{ startBlocked || '选择已就绪，开始前还需确认本次推理影响。' }}</p><button ref="startButton" class="evaluation-button primary" :disabled="!!startBlocked || formLocked" aria-describedby="evaluation-start-help" @click="reviewPlan"><ClipboardCheck :size="16" aria-hidden="true" />{{ checking ? '正在核对…' : submitting ? '正在提交…' : '确认并开始评测' }}</button></div>
    </section>

    <section class="panel evaluation-history" aria-labelledby="evaluation-history-title" :aria-busy="historyLoading">
      <div class="evaluation-section-head"><div><h3 id="evaluation-history-title">运行记录</h3><p>最近 30 次评测，选择一条查看完整结果。</p></div><span class="evaluation-muted">{{ runs.length }} 条记录</span></div>
      <p v-if="historyError" class="evaluation-alert danger" role="alert">{{ historyError }}</p>
      <div v-if="!runs.length" class="evaluation-empty"><ClipboardCheck :size="26" aria-hidden="true" /><p>{{ historyLoading ? '正在读取运行记录…' : historyError ? '运行记录暂不可用。' : '暂无评测记录，完成选择后可开始第一次评测。' }}</p></div>
      <div v-else class="evaluation-table-scroll" role="region" aria-label="评测运行记录表，可横向滚动" tabindex="0"><table class="evaluation-table"><thead><tr><th scope="col">创建时间 / ID</th><th scope="col">套件 / 模型</th><th scope="col">运行状态</th><th scope="col">评测结论 / 覆盖</th><th scope="col">操作</th></tr></thead><tbody>
        <tr v-for="run in runs" :key="run.id" :class="{ selected: selectedId === run.id }" :data-run-id="run.id"><td>{{ evaluationTime(run.created_at) }}<code>{{ run.id }}</code></td><td>{{ suiteLabels[run.suite] || run.suite }} · {{ run.repeats }} 次<small :title="run.models.join('、')">{{ run.models.length }} 个模型 · {{ run.version }}</small></td><td><span class="evaluation-badge">{{ runStatusLabels[run.status] || run.status }}</span></td><td><span class="evaluation-badge" :data-verdict="evaluationVerdict(run)">{{ verdictLabels[evaluationVerdict(run)] }}</span><small>通过 {{ run.summary.passed }} / {{ run.summary.total }} · 失败 {{ run.summary.failed }} · 跳过 {{ run.summary.skipped }} · 待定 {{ run.summary.pending }}</small></td><td><button class="evaluation-button" :aria-pressed="selectedId === run.id" :aria-label="`查看评测 ${run.id}`" @click="selectRun(run.id, true)">{{ selectedId === run.id ? '正在查看' : '查看详情' }}</button></td></tr>
      </tbody></table></div>
    </section>

    <section v-if="selectedId" ref="detailPanel" class="panel evaluation-detail" aria-labelledby="evaluation-detail-title" :aria-busy="detailLoading" tabindex="-1">
      <div class="evaluation-section-head"><div><h3 id="evaluation-detail-title">评测详情</h3><code>{{ selectedId }}</code><p v-if="detailAt">最后成功读取 {{ detailAt }}{{ detail && isActiveRun(detail) ? ' · 状态自动更新' : '' }}</p></div><div class="evaluation-actions"><button class="evaluation-button" :disabled="detailLoading || submitting" @click="selectRun(selectedId)"><RefreshCw :size="14" aria-hidden="true" />查询状态</button><template v-if="detail"><a class="evaluation-button" :href="`/api/evaluations/${encodeURIComponent(detail.id)}/export?format=json`" :aria-disabled="!!exporting" download @click.prevent="exportRun('json')"><Download :size="14" aria-hidden="true" />{{ exporting === 'json' ? '导出中…' : '导出 JSON' }}</a><a class="evaluation-button" :href="`/api/evaluations/${encodeURIComponent(detail.id)}/export?format=md`" :aria-disabled="!!exporting" download @click.prevent="exportRun('md')"><Download :size="14" aria-hidden="true" />{{ exporting === 'md' ? '导出中…' : '导出 Markdown' }}</a></template></div></div>
      <p v-if="detailError" class="evaluation-alert danger" role="alert">{{ detailError }}</p>
      <p v-if="!detail" class="evaluation-empty">{{ detailLoading ? '正在读取评测详情…' : '尚未取得该任务的详情，请查询状态。' }}</p>
      <template v-else>
        <div class="evaluation-result-head"><div><span class="evaluation-badge">{{ runStatusLabels[detail.status] }}</span> <span class="evaluation-badge" :data-verdict="detailVerdict">{{ verdictLabels[detailVerdict] }}</span><p>{{ suiteLabels[detail.suite] || detail.suite }} · 重复 {{ detail.repeats }} 次 · 版本 {{ detail.version }}</p><p>模型：{{ detail.models.join('、') }}</p></div><button v-if="isActiveRun(detail)" class="evaluation-button danger-button" :disabled="detail.status === 'cancelling' || !!cancelBusy || cancelUncertainIds.includes(detail.id)" @click="cancelRun(detail)">{{ cancelBusy === detail.id ? '提交取消中…' : detail.status === 'cancelling' ? '等待当前请求结束' : cancelUncertainIds.includes(detail.id) ? '取消结果待确认' : '取消后续用例' }}</button></div>
        <p v-if="detail.status === 'cancelling'" class="evaluation-alert warning" role="status">取消已受理，但当前推理尚未结束。后续用例会停止，当前请求最长可能需要约 180 秒，期间仍占用资源。</p>
        <p v-if="['interrupted', 'cancelled'].includes(detail.status)" class="evaluation-alert warning">{{ detail.status === 'interrupted' ? '执行中断，部分请求的最终结果可能未知。' : '任务已取消，保留已完成用例。' }}未执行或中断的部分不算通过；不会自动补跑。</p>
        <p v-if="detail.error" class="evaluation-alert danger" role="alert">{{ detail.error }}</p>
        <p v-if="detailVerdict === 'incomplete'" class="evaluation-reason">本次证据不足以判定全部通过。请核对跳过、待定、中断用例与服务端结论（{{ verdictLabels[detail.verdict] }}）。</p>
        <div class="evaluation-counts" aria-label="用例汇总"><div><strong>{{ detail.summary.total }}</strong><span>总用例</span></div><div><strong>{{ detail.summary.passed }}</strong><span>通过</span></div><div><strong>{{ detail.summary.failed }}</strong><span>失败</span></div><div><strong>{{ detail.summary.skipped }}</strong><span>跳过</span></div><div><strong>{{ detail.summary.pending }}</strong><span>待定</span></div><div><strong>{{ runningCases }} / {{ interruptedCases }}</strong><span>执行中 / 中断</span></div></div>
        <div class="evaluation-progress"><progress :value="Math.min(finishedCases, detail.summary.total)" :max="detail.summary.total || 1" aria-label="已结束用例，含跳过与中断" /><span>已结束 {{ finishedCases }} / {{ detail.summary.total }} 个用例（含跳过与中断，不代表通过率）</span></div>
        <div class="evaluation-timing"><span>创建 {{ evaluationTime(detail.created_at) }}</span><span>开始 {{ evaluationTime(detail.started_at) }}</span><span>结束 {{ evaluationTime(detail.finished_at) }}</span></div>
        <section class="evaluation-latency" aria-label="网关墙钟耗时统计"><div><h4>网关墙钟耗时</h4><p>包含请求等待与推理全过程，不是 tokens/s（TPS）。有效耗时样本 {{ detail.summary.latency_ms.count }} 个。</p></div><dl><div v-for="item in latencyStats" :key="item.label"><dt>{{ item.label }}</dt><dd>{{ item.text }}</dd></div></dl></section>
        <div class="evaluation-section-head case-heading"><h4>逐项结果</h4><div class="evaluation-actions"><input v-model="caseSearch" type="search" aria-label="搜索用例、模型或原因" placeholder="搜索用例、模型或原因" /><select v-model="caseFilter" aria-label="筛选用例状态"><option value="">全部状态</option><option v-for="(label, status) in caseStatusLabels" :key="status" :value="status">{{ label }}</option></select></div></div>
        <p class="evaluation-muted">{{ visibleCases.length }} / {{ detail.cases.length }} 个用例。展开后查看实际输出、预期与原始证据。</p>
        <p v-if="!visibleCases.length" class="evaluation-empty">{{ detail.cases.length ? '没有符合筛选条件的用例。' : '尚无用例明细；排队中或未生成结果不代表通过。' }}</p>
        <div class="evaluation-cases"><details v-for="item in visibleCases" :key="`${detail.id}:${item.id}`" class="evaluation-case" :data-case-id="item.id"><summary><span class="evaluation-case-title"><strong>{{ item.name }}</strong><small>{{ item.model }} · {{ item.scope === 'execution' ? '执行自检' : '质量核对' }}</small></span><span class="evaluation-badge" :data-verdict="item.status === 'passed' ? 'passed' : item.status === 'failed' ? 'failed' : 'pending'">{{ caseStatusLabels[item.status] }}</span><span class="evaluation-muted">{{ latencyText(item.latency_ms) }}</span><span v-if="item.reason || item.status === 'skipped'" class="evaluation-case-reason">{{ item.reason || '已跳过，服务端未提供原因' }}</span></summary>
          <div class="evaluation-case-body"><p v-if="item.reason" class="evaluation-reason">原因：{{ item.reason }}</p><div class="evaluation-evidence"><div><h5>预期 / 判定标准</h5><pre tabindex="0" :aria-label="`${item.name} 预期`">{{ evidenceText(item.expected) }}</pre></div><div><h5>实际输出</h5><pre tabindex="0" :aria-label="`${item.name} 实际输出`">{{ item.actual === undefined ? '尚无实际输出（待执行、跳过或中断不算通过）' : evidenceText(item.actual) }}</pre></div></div><dl class="evaluation-case-meta"><dt>用例 ID</dt><dd><code>{{ item.id }}</code></dd><dt>输入 SHA-256</dt><dd><code>{{ item.input_sha256 || '未记录' }}</code></dd></dl><details class="evaluation-raw"><summary>参数、样例与指标</summary><h5>参数</h5><pre tabindex="0">{{ evidenceText(item.parameters) }}</pre><template v-if="item.sample !== undefined"><h5>样例</h5><pre tabindex="0">{{ evidenceText(item.sample) }}</pre></template><template v-if="item.metrics !== undefined"><h5>指标</h5><pre tabindex="0">{{ evidenceText(item.metrics) }}</pre></template></details></div>
        </details></div>
        <details :key="detail.id" class="evaluation-raw evaluation-snapshot"><summary>本次运行快照与配置证据</summary><p class="evaluation-muted">以下为本次评测保存的快照，用于复核当时的环境与参数。</p><pre tabindex="0" aria-label="评测运行快照">{{ evidenceText(detail.snapshot) }}</pre></details>
      </template>
    </section>

    <dialog ref="confirmDialog" class="evaluation-dialog" aria-labelledby="evaluation-confirm-title" aria-describedby="evaluation-confirm-impact" @cancel.prevent="closeConfirmation">
      <template v-if="confirmation"><h3 id="evaluation-confirm-title">确认本次真实推理</h3><p>{{ suiteLabels[confirmation.suite] }} · {{ confirmation.models.length }} 个模型 · 每个用例 {{ confirmation.repeats }} 次</p><p class="evaluation-confirm-models">{{ confirmation.models.join('、') }}</p><div id="evaluation-confirm-impact" class="evaluation-impact"><TriangleAlert :size="21" aria-hidden="true" /><div><strong>会占用设备与网关资源，可能影响正在使用模型的请求。</strong><p>取消仅停止后续用例；当前推理会继续到结束，最长可能需要约 180 秒。离开或刷新页面不会取消任务。</p></div></div><label class="evaluation-consent"><input v-model="impactAccepted" type="checkbox" :disabled="submitting" />我已了解资源占用与取消延迟，确认执行本次真实推理。</label><p v-if="operationState.busy" class="evaluation-reason" role="status">管理操作尚未结束，请返回并等待操作完成。</p><div class="evaluation-dialog-actions"><button class="evaluation-button" :disabled="submitting" autofocus @click="closeConfirmation">返回调整</button><button class="evaluation-button primary" :disabled="!impactAccepted || submitting || operationState.busy" @click="startEvaluation"><LoaderCircle v-if="submitting" :size="16" class="spin" aria-hidden="true" />{{ submitting ? '正在核对并提交…' : '开始真实推理' }}</button></div></template>
    </dialog>
  </div>
</template>

<style scoped>
.evaluation-page { display: grid; gap: 18px; min-width: 0; font-size: 13px; line-height: 1.65; }
.evaluation-page h2, .evaluation-page h3, .evaluation-page h4, .evaluation-page h5, .evaluation-page p { margin: 0; }
.evaluation-page h2 { font-size: 22px; }.evaluation-page h3 { font-size: 16px; }.evaluation-page h4 { font-size: 14px; }.evaluation-page h5 { font-size: 12px; margin-bottom: 8px; }
.evaluation-intro, .evaluation-section-head, .evaluation-start-row, .evaluation-result-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.evaluation-intro p, .evaluation-section-head p { color: #5f7079; margin-top: 5px; font-size: 12px; }
.evaluation-intro > div, .evaluation-section-head > div, .evaluation-result-head > div, .evaluation-alert > div { min-width: 0; }
.evaluation-page .panel { min-width: 0; padding: 22px; border-radius: 12px; }
.evaluation-section-head { margin-bottom: 18px; flex-wrap: wrap; }.evaluation-section-head code, .evaluation-alert code { overflow-wrap: anywhere; }
.evaluation-button { display: inline-flex; align-items: center; justify-content: center; gap: 7px; min-height: 38px; padding: 8px 12px; border: 1px solid #cbd7d6; border-radius: 7px; background: #fff; color: #344e48; font-size: 12px; font-weight: 600; flex-shrink: 0; text-decoration: none; }
.evaluation-button:hover:not(:disabled) { background: #edf6f2; border-color: #91b9a8; }.evaluation-button.primary { background: #187651; border-color: #187651; color: #fff; }.evaluation-button.primary:hover:not(:disabled) { background: #125b3e; }
.evaluation-button.danger-button { color: #a03e34; border-color: #e6c3bb; }.evaluation-button:disabled, .evaluation-button[aria-disabled="true"] { cursor: not-allowed; opacity: .5; }
.evaluation-page :is(a, button, input, select, summary, pre, [tabindex]):focus-visible { outline: 3px solid #187651; outline-offset: 3px; }
.evaluation-page input:not([type="checkbox"]):not([type="radio"]), .evaluation-page select { min-height: 38px; min-width: 0; padding: 8px 10px; border: 1px solid #cbd7d6; border-radius: 7px; background: #fff; color: var(--ink); font-size: 12px; }
.evaluation-page input:is([type="checkbox"], [type="radio"]) { width: 17px; height: 17px; accent-color: #187651; margin: 3px 0 0; flex: 0 0 17px; cursor: pointer; }
.evaluation-page input:disabled { cursor: not-allowed; }.evaluation-muted { color: #60717a; font-size: 12px; }.evaluation-reason { color: #925524; font-size: 12px; overflow-wrap: anywhere; }
.evaluation-alert { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; padding: 14px 16px; border: 1px solid #c5dfd0; border-radius: 9px; background: #edf7f1; color: #2d5c46; overflow-wrap: anywhere; }
.evaluation-alert p { margin-top: 4px; }.evaluation-alert.danger { background: #fff2ef; border-color: #efc6bd; color: #913d35; }.evaluation-alert.warning { background: #fff8e9; border-color: #e8d4a8; color: #79531f; }
.evaluation-create > .evaluation-alert, .evaluation-detail > .evaluation-alert, .evaluation-history > .evaluation-alert { margin-bottom: 16px; }
.evaluation-fieldset { min-width: 0; border: 0; border-top: 1px solid var(--line); padding: 18px 0; margin: 14px 0 0; }.evaluation-fieldset legend { padding: 0 12px 0 0; font-size: 14px; font-weight: 600; }.evaluation-fieldset legend span { margin-left: 10px; font-weight: 400; }
.evaluation-suites { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }.evaluation-suite, .evaluation-model { display: flex; align-items: flex-start; gap: 10px; padding: 14px; border: 1px solid #d5e0dc; border-radius: 8px; cursor: pointer; background: #fff; min-width: 0; }
.evaluation-suite.chosen, .evaluation-model.chosen { border-color: #288760; background: #f0f8f3; box-shadow: inset 0 0 0 1px #288760; }.evaluation-suite span, .evaluation-model span { min-width: 0; }.evaluation-suite strong, .evaluation-model strong { display: block; overflow-wrap: anywhere; font-size: 13px; }.evaluation-suite small, .evaluation-model small { display: block; font-size: 12px; color: #5f7079; margin-top: 5px; overflow-wrap: anywhere; }
.evaluation-model.unavailable { background: #f6f7f7; cursor: default; }.evaluation-model .evaluation-reason { color: #925524; }.evaluation-repeat { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-top: 16px; }.evaluation-repeat span { color: #60717a; font-size: 12px; }
.evaluation-filters { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 10px; }.evaluation-search { flex: 1 1 220px; display: flex; align-items: center; border: 1px solid #cbd7d6; border-radius: 7px; padding-left: 10px; color: #60717a; background: white; }.evaluation-search input { flex: 1; width: 100%; border: 0 !important; }.evaluation-device { display: flex; align-items: center; gap: 7px; }.evaluation-device select { max-width: 200px; }
.evaluation-models { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 14px; max-height: 460px; overflow-y: auto; padding: 4px; }.evaluation-selected { margin-top: 12px !important; color: #3b5f50; font-size: 12px; overflow-wrap: anywhere; }
.evaluation-impact { display: flex; gap: 12px; padding: 16px; background: #fff8e9; border: 1px solid #ead9b3; border-radius: 9px; color: #79531f; }.evaluation-impact > svg { flex-shrink: 0; margin-top: 2px; }.evaluation-impact p { margin-top: 5px; font-size: 12px; line-height: 1.8; }
.evaluation-start-row { margin-top: 18px; }.evaluation-empty { padding: 28px 12px; text-align: center; color: #60717a; }.evaluation-empty svg { margin-bottom: 8px; }.evaluation-empty p { margin-top: 6px; }
.evaluation-table-scroll { overflow-x: auto; }.evaluation-table { border-collapse: collapse; width: 100%; min-width: 780px; text-align: left; font-size: 12px; }.evaluation-table th { color: #596d73; background: #f6f8f7; font-weight: 600; }.evaluation-table :is(th, td) { padding: 12px 10px; border-bottom: 1px solid #e4eae7; vertical-align: top; }.evaluation-table tr.selected { background: #f0f8f3; }.evaluation-table td code, .evaluation-table td small { display: block; font-size: 11px; color: #60717a; margin-top: 5px; }
.evaluation-badge { display: inline-block; font-size: 12px; font-weight: 600; color: #50616e; background: #edf1f4; padding: 3px 8px; border-radius: 5px; white-space: nowrap; }.evaluation-badge[data-verdict="passed"] { background: #e6f4eb; color: #226d43; }.evaluation-badge[data-verdict="failed"] { background: #fbe9e5; color: #a33c31; }.evaluation-badge[data-verdict="incomplete"] { background: #fff0d5; color: #875c1f; }
.evaluation-actions { display: flex; gap: 8px; flex-wrap: wrap; }.evaluation-result-head { align-items: flex-start; margin-bottom: 18px; }.evaluation-result-head p { margin-top: 7px; color: #60717a; font-size: 12px; overflow-wrap: anywhere; }
.evaluation-counts { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; margin: 18px 0; }.evaluation-counts > div { border: 1px solid #dce5e0; border-radius: 8px; padding: 14px; }.evaluation-counts strong { display: block; font-size: 23px; font-weight: 650; }.evaluation-counts span { color: #60717a; font-size: 12px; }
.evaluation-progress { display: grid; gap: 7px; color: #60717a; font-size: 12px; }.evaluation-progress progress { width: 100%; height: 9px; accent-color: #408976; }.evaluation-timing { display: flex; flex-wrap: wrap; gap: 8px 20px; font-size: 11px; color: #60717a; margin: 14px 0 20px; }
.evaluation-latency { background: #f5f8f7; border: 1px solid #dce5e0; border-radius: 9px; padding: 16px; }.evaluation-latency p { color: #60717a; font-size: 12px; margin-top: 5px; }.evaluation-latency dl { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin: 16px 0 0; }.evaluation-latency dt { font-size: 11px; color: #60717a; }.evaluation-latency dd { margin: 5px 0 0; font-size: 14px; overflow-wrap: anywhere; }
.case-heading { margin: 24px 0 10px; }.case-heading input { width: 220px; }.evaluation-cases { display: grid; gap: 10px; margin-top: 12px; }.evaluation-case { border: 1px solid #dce5e0; border-radius: 8px; overflow: clip; }.evaluation-case > summary { display: flex; align-items: center; gap: 12px; padding: 14px; cursor: pointer; }.evaluation-case > summary::before { content: '+'; font-size: 18px; color: #396650; flex-shrink: 0; }.evaluation-case[open] > summary::before { content: '−'; }.evaluation-case-title { flex: 1; min-width: 0; }.evaluation-case-title strong, .evaluation-case-title small { display: block; overflow-wrap: anywhere; }.evaluation-case-title small { color: #60717a; font-size: 11px; margin-top: 4px; }.evaluation-case-reason { width: 24%; color: #925524; font-size: 12px; overflow-wrap: anywhere; }
.evaluation-case-body { padding: 16px; border-top: 1px solid #e4eae7; }.evaluation-evidence { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; margin-top: 12px; }.evaluation-evidence > div { min-width: 0; }.evaluation-page pre { max-height: 340px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; padding: 14px; margin: 0; border-radius: 7px; background: #f3f6f5; color: #263f3c; font: 12px/1.8 Consolas, 'Microsoft YaHei', monospace; }.evaluation-case-meta { display: grid; grid-template-columns: 100px minmax(0, 1fr); gap: 7px; font-size: 12px; margin: 16px 0; }.evaluation-case-meta dt { color: #60717a; }.evaluation-case-meta dd { margin: 0; overflow-wrap: anywhere; }
.evaluation-raw > summary { padding: 9px 0; color: #356c53; font-weight: 600; cursor: pointer; }.evaluation-raw h5 { margin-top: 12px; }.evaluation-snapshot { border-top: 1px solid #dce5e0; margin-top: 20px; padding-top: 10px; }.evaluation-snapshot pre { margin-top: 10px; }
.evaluation-dialog { width: min(580px, calc(100vw - 32px)); max-height: calc(100dvh - 32px); overflow-y: auto; border: 1px solid #cbd9d0; border-radius: 14px; padding: 26px; color: var(--ink); box-shadow: 0 22px 70px #10242b38; }.evaluation-dialog::backdrop { background: #10242b88; }.evaluation-dialog h3 { font-size: 19px; }.evaluation-dialog > p { margin-top: 10px; color: #5f7079; }.evaluation-confirm-models { overflow-wrap: anywhere; max-height: 120px; overflow-y: auto; font-size: 12px; }.evaluation-dialog .evaluation-impact { margin-top: 18px; }.evaluation-consent { display: flex; align-items: flex-start; gap: 10px; margin: 20px 0; cursor: pointer; }.evaluation-dialog-actions { display: flex; justify-content: flex-end; gap: 10px; }
@media (max-width: 1150px) { .evaluation-models { grid-template-columns: repeat(2, minmax(0, 1fr)); }.evaluation-counts { grid-template-columns: repeat(3, minmax(0, 1fr)); }.evaluation-suites { grid-template-columns: 1fr; }.evaluation-case > summary { flex-wrap: wrap; }.evaluation-case-reason { width: 100%; padding-left: 24px; } }
@media (max-width: 760px) { .evaluation-page .panel { padding: 16px; }.evaluation-intro, .evaluation-start-row, .evaluation-result-head { align-items: stretch; flex-direction: column; }.evaluation-intro h2 { font-size: 20px; }.evaluation-models { grid-template-columns: 1fr; }.evaluation-filters .evaluation-search { flex-basis: 100%; }.evaluation-actions { width: 100%; }.evaluation-actions > input { flex: 1; width: 140px; }.evaluation-counts { gap: 7px; }.evaluation-counts > div { padding: 10px; }.evaluation-counts strong { font-size: 20px; }.evaluation-latency dl { grid-template-columns: repeat(2, minmax(0, 1fr)); }.evaluation-evidence { grid-template-columns: 1fr; }.evaluation-dialog { padding: 20px; }.evaluation-dialog-actions { flex-wrap: wrap; }.evaluation-dialog-actions button { flex: 1; }.evaluation-case > summary { gap: 8px; }.evaluation-case-title { flex-basis: calc(100% - 34px); }.evaluation-case-meta { grid-template-columns: 1fr; }.evaluation-repeat span { flex-basis: 100%; } }
@media (prefers-reduced-motion: reduce) { .evaluation-page .spin { animation: none; } }
</style>
