<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  Activity,
  CircleCheck,
  Cpu,
  HardDrive,
  LoaderCircle,
  MemoryStick,
  Radio,
  TriangleAlert,
  X,
} from '@lucide/vue'
import { describeError } from '../api'
import { loadingOverview, models, overview, overviewError, refreshOverview } from '../store'
import type { ModelRecord } from '../types'
import { operations, operationState } from '../operations'
import RuntimeSafetyNotice from '../RuntimeSafetyNotice.vue'

const router = useRouter()
const modeModal = ref(false)
const targetMode = ref('copaw')
const switching = ref(false)
const operationMessage = ref('')
const operationError = ref('')
const nowMs = ref(Date.now())
let elapsedTimer: ReturnType<typeof setInterval> | undefined

const system = computed(() => overview.value?.host.system || {})
const platform = computed(() => overview.value?.host.platform)
const allowModeSwitch = computed(() => platform.value?.allow_mode_switch !== false)
const gateway = computed(() => overview.value?.gateway)
const activeMode = computed(() => gateway.value?.active_mode?.trim() || 'unknown')
const legacyModeNames: Record<string, string> = {
  copaw: 'CoPaw 独占模式',
  'spark-x1': 'Spark X1 独占模式',
  qwen38: 'Qwen3.8 独占模式', qwen36: 'Qwen3.6 双卡独占模式',
  'qwen-vl': 'Qwen3-VL 独占模式',
  'glm-ocr': 'GLM-OCR 独占模式',
  'bge-m3': 'BGE-M3 独占模式',
  reranker: 'BGE Reranker 独占模式',
  cosyvoice: 'CosyVoice3 独占模式',
}
const legacyModeOptions: Array<[string, string]> = [
  ['copaw', 'CoPaw Flash 9B · 2 槽'],
  ['spark-x1', 'Spark X1 4B · 2 槽 / 256K'],
  ['qwen38', 'Qwen3.8 27B · 自动适配单卡 / 双卡'],
  ['qwen36', 'Qwen3.6 35B A3B · 双卡 / 256K'],
  ['qwen-vl', 'Qwen3-VL 8B · 2 槽'],
  ['glm-ocr', 'GLM-OCR 0.9B · 2 槽'],
  ['bge-m3', 'BGE-M3 · 10 槽'],
  ['reranker', 'BGE Reranker · 10 槽'],
  ['cosyvoice', 'CosyVoice3 0.5B · 1 槽 / 2K'],
]
const modeOptions = computed<Array<[string, string]>>(() => {
  const grouped = new Map<string, string[]>()
  for (const model of models.value.data) {
    const label = String(model.metadata?.display_name || model.id)
    for (const mode of model.modes || []) grouped.set(mode, [...(grouped.get(mode) || []), label])
  }
  return grouped.size
    ? [...grouped].map(([mode, labels]) => [mode, [...new Set(labels)].join(' + ')])
    : legacyModeOptions
})
const modeNames = computed<Record<string, string>>(() => ({
  ...legacyModeNames,
  ...Object.fromEntries(modeOptions.value.map(([mode, label]) => [mode, label])),
}))
const modeReady = computed(() => !!gateway.value && activeMode.value !== 'unknown')
const modeLabel = computed(() => {
  if (platform.value?.mode_label) return platform.value.mode_label
  if (modeNames.value[activeMode.value]) return modeNames.value[activeMode.value]
  if (!gateway.value && loadingOverview.value) return '正在读取运行模式'
  if (activeMode.value === 'unknown') return '运行模式读取失败，请刷新状态'
  return `未识别的运行模式：${activeMode.value}`
})
const modeProgress = computed(() => operationState.job?.kind === 'mode-switch' ? operationState.job.progress : undefined)
const switchElapsed = computed(() => {
  const started = operationState.job?.started_at ? Date.parse(operationState.job.started_at) : NaN
  const seconds = Number.isFinite(started) ? Math.max(0, Math.floor((nowMs.value - started) / 1000)) : Math.floor(modeProgress.value?.elapsed_seconds || 0)
  const minutes = Math.floor(seconds / 60)
  return minutes ? `${minutes} 分 ${String(seconds % 60).padStart(2, '0')} 秒` : `${seconds} 秒`
})
const switchPercent = computed(() => modeProgress.value?.total ? Math.round((modeProgress.value.completed || 0) / modeProgress.value.total * 100) : 0)
const onlineContainers = computed(() => overview.value?.host.containers.filter(item => item.state === 'running').length || 0)
const coreIds = ['copaw-flash-9b', 'spark-x1-4b', 'qwen3.8-27b', 'qwen3.6-35b-a3b', 'qwen3-vl-8b', 'glm-ocr-0.9b', 'bge-m3', 'bge-reranker-v2-m3', 'cosyvoice3-0.5b']
const coreModels = computed(() => {
  const selected = models.value.data.filter(model => model.metadata?.dashboard === true)
  return selected.length ? selected : coreIds.map(id => models.value.data.find(model => model.id === id)).filter(Boolean) as ModelRecord[]
})

const modelNames: Record<string, string> = {
  'copaw-flash-9b': 'CoPaw Flash 9B',
  'spark-x1-4b': 'Spark X1 4B',
  'cosyvoice3-0.5b': 'CosyVoice3 0.5B',
  'qwen3.8-27b': 'Qwen3.8 27B', 'qwen3.6-35b-a3b': 'Qwen3.6 35B A3B 双卡',
  'qwen3-vl-8b': 'Qwen3 VL 8B',
  'glm-ocr-0.9b': 'GLM-OCR 0.9B',
  'bge-m3': 'BGE M3',
  'bge-reranker-v2-m3': 'BGE Reranker V2 M3',
}

const capabilityLabels: Record<string, string> = {
  chat: '对话', embeddings: '向量', rerank: '重排',
  document_analysis: '文档分析',
}

function bytes(value = 0) {
  if (!value) return '—'
  const gb = value / 1024 ** 3
  return `${gb >= 100 ? gb.toFixed(0) : gb.toFixed(1)} GB`
}

function latency(id: string) {
  const value = gateway.value?.models[id]?.latency_ms
  return value == null ? '—' : `${value.toFixed(0)} ms`
}

function device(model: ModelRecord) {
  return String(model.metadata?.device || '—')
}

async function switchMode() {
  if (!allowModeSwitch.value) return
  if (operationState.busy) return
  switching.value = true
  operationError.value = ''
  operationMessage.value = `正在切换到 ${targetMode.value}，请勿关闭盒子…`
  try {
    await operations.submit('/api/mode/switch', { method: 'POST', body: JSON.stringify({ mode: targetMode.value }) }, '切换运行模式')
    operationMessage.value = '模式切换完成，模型状态已重新校验。'
    modeModal.value = false
    await refreshOverview()
  } catch (error) {
    operationError.value = describeError(error)
    operationMessage.value = ''
    await refreshOverview().catch(() => undefined)
  } finally {
    switching.value = false
  }
}

function openModeModal() {
  if (!allowModeSwitch.value) return
  targetMode.value = modeOptions.value.find(([mode]) => mode !== activeMode.value)?.[0] || activeMode.value
  modeModal.value = true
}

onMounted(() => {
  if (!overview.value) refreshOverview().catch(() => undefined)
  elapsedTimer = setInterval(() => { nowMs.value = Date.now() }, 1000)
})
onUnmounted(() => { if (elapsedTimer) clearInterval(elapsedTimer) })
</script>

<template>
  <section class="page-body">
    <RuntimeSafetyNotice />
    <div v-if="platform?.notice" class="notice" role="status"><Cpu :size="17" /><strong>{{ platform.name }}</strong> · {{ platform.notice }}</div>
    <div v-if="overviewError" class="notice error-notice" role="alert">状态读取失败：{{ overviewError }}</div>
    <div v-if="operationMessage" class="notice success-notice"><CircleCheck :size="16" />{{ operationMessage }}</div>
    <div v-if="operationError" class="notice error-notice"><TriangleAlert :size="16" />{{ operationError }}</div>

    <section class="mode-banner">
      <div class="mode-copy">
        <span class="mode-icon"><Radio :size="21" /></span>
        <div><small>当前运行模式</small><strong>{{ modeLabel }}</strong></div>
        <span class="mode-code">{{ activeMode }}</span>
      </div>
      <div class="mode-note"><CircleCheck :size="17" /> {{ platform?.external_management ? '模型由外部运行环境管理' : `配置模式与实际容器状态${overview?.host.modelctl?.healthy ? '一致' : '待核验'}` }}</div>
      <button v-if="allowModeSwitch" :disabled="operationState.busy || !modeReady" @click="openModeModal">切换模式</button>
    </section>

    <section class="metric-grid" aria-label="资源概览">
      <article class="metric-card">
        <div class="metric-head"><span class="metric-icon cyan"><Cpu :size="20" /></span><span class="trend good">{{ (system.cpu_percent || 0) < 85 ? '正常' : '较高' }}</span></div>
        <strong>{{ system.cpu_percent ?? '—' }}<em v-if="system.cpu_percent != null">%</em></strong><p>CPU 使用率</p>
        <div class="meter"><i :style="{ width: `${system.cpu_percent || 0}%` }"></i></div><small>{{ system.cpu_cores || '—' }} 核 ARM64</small>
      </article>
      <article class="metric-card">
        <div class="metric-head"><span class="metric-icon violet"><MemoryStick :size="20" /></span><span class="trend good">{{ (system.memory_percent || 0) < 85 ? '充足' : '较高' }}</span></div>
        <strong>{{ bytes(system.memory_used) }}</strong><p>内存占用</p>
        <div class="meter violet-meter"><i :style="{ width: `${system.memory_percent || 0}%` }"></i></div><small>总计 {{ bytes(system.memory_total) }}</small>
      </article>
      <article class="metric-card">
        <div class="metric-head"><span class="metric-icon amber"><HardDrive :size="20" /></span><span class="trend good">{{ (system.disk_percent || 0) < 85 ? '充足' : '偏满' }}</span></div>
        <strong>{{ bytes(system.disk_used) }}</strong><p>磁盘占用</p>
        <div class="meter amber-meter"><i :style="{ width: `${system.disk_percent || 0}%` }"></i></div><small>剩余 {{ bytes(system.disk_free) }}</small>
      </article>
      <article class="metric-card accent-card">
        <div class="metric-head"><span class="metric-icon green"><Activity :size="20" /></span><span class="trend live"><span></span> LIVE</span></div>
        <strong>{{ onlineContainers }} / {{ overview?.host.containers.length || '—' }}</strong><p>{{ platform?.service_kind === 'systemd' ? '运行中控制面服务' : '运行中容器' }}</p>
        <div class="service-dots"><i v-for="n in Math.max(onlineContainers, 1)" :key="n"></i></div><small>网关状态：{{ gateway?.status || '读取中' }}</small>
      </article>
    </section>

    <article v-if="overview?.host.accelerators?.length || overview?.host.accelerator_error" class="panel ascend-panel">
      <div class="panel-head"><div><h2>昇腾 NPU</h2><p>按逻辑设备展示独立内存、温度和计算利用率</p></div></div>
      <div v-if="overview.host.accelerator_error" class="notice error-notice">{{ overview.host.accelerator_error }}</div>
      <div class="table-wrap"><table><thead><tr><th>设备</th><th>物理卡 / 芯片</th><th>计算利用率</th><th>内存占用</th><th>温度</th><th>健康</th></tr></thead>
        <tbody><tr v-for="npu in overview.host.accelerators" :key="npu.device"><td><strong>NPU {{ npu.device }} · {{ npu.name }}</strong></td><td>{{ npu.card }} / {{ npu.chip }}</td><td>{{ npu.aicore_percent }}%</td><td>{{ (npu.memory_used_mb / 1024).toFixed(1) }} / {{ (npu.memory_total_mb / 1024).toFixed(1) }} GiB</td><td>{{ npu.temperature_c }} °C</td><td><span :class="['status-pill', npu.health !== 'OK' && 'muted-pill']">{{ npu.health }}</span></td></tr></tbody>
      </table></div>
    </article>

    <section class="content-grid single-panel">
      <article class="panel model-panel">
        <div class="panel-head">
          <div><h2>核心模型状态</h2><p>当前模式下的关键推理服务</p></div>
          <button class="text-button" @click="router.push('/models')">查看全部 <span>→</span></button>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>模型</th><th>能力</th><th>设备</th><th>健康延迟</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="model in coreModels" :key="model.id">
                <td><strong>{{ modelNames[model.id] || model.id }}</strong><code>{{ model.id }}</code></td>
                <td>{{ capabilityLabels[model.capability] || model.capability }}</td><td>{{ device(model) }}</td><td class="mono">{{ latency(model.id) }}</td>
                <td><span :class="['status-pill', model.status !== 'online' && 'muted-pill']"><i></i>{{ model.status === 'online' ? '在线' : model.status === 'inactive' ? '未激活' : model.status }}</span></td>
              </tr>
              <tr v-if="!coreModels.length"><td colspan="5" class="empty-row">{{ loadingOverview ? '正在读取模型状态…' : '暂无模型状态' }}</td></tr>
            </tbody>
          </table>
        </div>
      </article>

    </section>

    <div v-if="modeModal" class="modal-backdrop" @click.self="!switching && (modeModal = false)">
      <section class="modal-card" role="dialog" aria-modal="true" aria-labelledby="mode-title">
        <button class="modal-close" :disabled="switching" aria-label="关闭" @click="modeModal = false"><X :size="17" /></button>
        <span class="modal-symbol"><Radio :size="24" /></span>
        <h2 id="mode-title">切换运行模式</h2>
        <p>切换会暂停当前互斥模型，启动目标模式并等待容器健康检查；失败时模型控制器恢复原模式。</p>
        <label class="mode-select">目标模式<select v-model="targetMode" :disabled="switching"><option v-for="([value, label]) in modeOptions" :key="value" :value="value">{{ label }}</option></select></label>
        <div class="impact-box"><strong>影响范围</strong><span>切换期间互斥模型接口会短暂不可用；控制面与 RAGFlow 保持运行。当前模式不会重复切换。</span></div>
        <div v-if="switching || operationError" :class="['mode-switch-progress', operationError && 'failed']" role="status" aria-live="polite">
          <div><strong>{{ operationError ? '切换未完成' : modeProgress?.stage || '正在提交切换任务' }}</strong><span v-if="switching">已用时 {{ switchElapsed }}</span></div>
          <p v-if="modeProgress?.current_model">当前模型：<code>{{ modeProgress.current_model }}</code></p>
          <progress v-if="modeProgress?.total" :value="modeProgress.completed || 0" :max="modeProgress.total"></progress>
          <small v-if="modeProgress?.total">实际推理验证 {{ modeProgress.completed || 0 }} / {{ modeProgress.total }}（{{ switchPercent }}%）</small>
          <p v-if="operationError" role="alert">{{ operationError }}</p>
          <small v-else>容器健康后还会执行固定推理；全部通过才会切换成功。</small>
        </div>
        <div class="modal-actions"><button class="secondary-button" :disabled="switching" @click="modeModal = false">取消</button><button class="primary-button" :disabled="operationState.busy || switching || targetMode === activeMode" @click="switchMode"><LoaderCircle v-if="switching" class="spin" :size="16" />确认切换到 {{ modeNames[targetMode] }}</button></div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.ascend-panel{margin-bottom:20px}.content-grid.single-panel{grid-template-columns:minmax(0,1fr)}
.mode-switch-progress{margin-top:14px;padding:13px 14px;border:1px solid #a9d3c4;border-radius:9px;background:#f3fbf7;color:#295f50}.mode-switch-progress.failed{border-color:#e3b1ae;background:#fff6f5;color:#913f3b}.mode-switch-progress>div{display:flex;justify-content:space-between;gap:12px;align-items:center}.mode-switch-progress strong{font-size:13px}.mode-switch-progress span,.mode-switch-progress small,.mode-switch-progress p{font-size:11px;line-height:1.55}.mode-switch-progress progress{width:100%;margin:9px 0 3px;accent-color:#3ea681}.mode-switch-progress code{font-size:11px}.mode-switch-progress [role=alert]{margin-top:6px;font-size:12px;overflow-wrap:anywhere}@media(max-width:600px){.mode-switch-progress>div{align-items:flex-start;flex-direction:column;gap:4px}}
</style>
