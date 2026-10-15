<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { LoaderCircle, ShieldAlert, X } from '@lucide/vue'
import { describeError } from '../api'
import { operations, operationState } from '../operations'
import { overview, refreshOverview } from '../store'

const emit = defineEmits<{ busy: [value: boolean] }>()
const modal = ref(false)
const confirmation = ref('')
const confirmedMode = ref('')
const job = computed(() => operationState.job?.kind === 'device-recovery' ? operationState.job : null)
const busy = computed(() => operationState.busy)
const querying = computed(() => operationState.querying)
const error = ref('')
const storageKey = 'modeldeck-device-recovery-job'
const mode = computed(() => overview.value?.host.modelctl?.configured_mode || overview.value?.gateway.active_mode || '')
const modeNames: Record<string, string> = {
  copaw: 'CoPaw Flash 9B', 'spark-x1': 'Spark X1 4B', qwen38: 'Qwen3.8 27B', qwen36: 'Qwen3.6 35B A3B 双卡',
  'qwen-vl': 'Qwen3-VL 8B', 'glm-ocr': 'GLM-OCR 0.9B', 'bge-m3': 'BGE-M3', reranker: 'BGE Reranker', cosyvoice: 'CosyVoice3 0.5B',
}
const validMode = computed(() => Object.hasOwn(modeNames, mode.value))
const evidence = computed(() => job.value?.progress?.evidence_id || String(job.value?.result?.evidence_id || ''))
const status = computed(() => ({ queued: '已提交', running: '执行中', succeeded: '完成', failed: '失败，已停止继续加载', unknown: '结果未知，请人工核查' }[job.value?.status || 'queued']))
const steps = computed(() => job.value?.progress?.steps || (Array.isArray(job.value?.result?.steps) ? job.value.result.steps as Array<{ at: string; stage: string }> : []))
const stage = computed(() => job.value?.progress?.stage || steps.value.at(-1)?.stage || (busy.value ? '正在读取操作进度' : status.value))

watch(busy, value => emit('busy', value), { immediate: true })
function open() { confirmedMode.value = mode.value; confirmation.value = ''; error.value = ''; modal.value = true }

async function track(id: string) {
  try {
    await operations.track(id)
    await refreshOverview().catch(() => undefined)
  } catch (reason) {
    error.value = describeError(reason)
  }
}

async function recover() {
  if (confirmation.value !== '恢复设备' || busy.value) return
  error.value = ''
  try {
    modal.value = false
    await operations.submit('/api/device/recover', { method: 'POST', body: JSON.stringify({ mode: confirmedMode.value, confirmation: confirmation.value }) }, '设备故障恢复')
    await refreshOverview().catch(() => undefined)
  } catch (reason) {
    error.value = describeError(reason)
  }
}

onMounted(() => {
  // Migrate a pending task from the earlier recovery-only UI; never resubmit it.
  try {
    const id = sessionStorage.getItem(storageKey)
    if (id && !operationState.busy && /^[a-zA-Z0-9_-]{1,128}$/.test(id)) {
      sessionStorage.setItem('modeldeck-management-job', JSON.stringify({ id, label: '设备故障恢复' }))
      sessionStorage.removeItem(storageKey)
      operations.resume()?.catch(reason => { error.value = describeError(reason) })
    }
  } catch { /* private browsers can disable storage */ }
})
</script>

<template>
  <section class="panel recovery-panel" aria-label="设备故障恢复">
    <div class="recovery-heading"><ShieldAlert :size="22" /><div><h2>设备故障恢复</h2><p>用于同卡模型异常、普通容器重启无效的情况。不会重启盒子或刷写驱动。</p></div>
      <button class="secondary-button recovery-action" :disabled="busy || !validMode" @click="open"><LoaderCircle v-if="busy" class="spin" :size="16" />设备故障恢复</button></div>
    <div v-if="job" class="recovery-progress" role="status" aria-live="polite">
      <strong>{{ status }}</strong><p>{{ stage }}</p>
      <p>任务 ID：<code>{{ job.id }}</code></p><p v-if="evidence">证据编号：<code>{{ evidence }}</code>（完整日志保存在盒子 state/device-recovery 目录）</p>
      <p v-if="job.status === 'succeeded'">请以任务记录中的验证范围为准。只有当前运行模型的实际推理验证全部通过，才会解除隔离；这不代替完整质量验收。</p>
      <p v-if="job.status === 'failed'">不要反复点击恢复。请先根据证据编号排查；停止失败时需人工介入。</p>
      <p v-if="job.result?.kernel_capture === false">本次任务自动采集的是容器日志；内核日志需由运维人员另行采集。</p>
      <ol v-if="steps.length"><li v-for="(step, index) in steps" :key="index">{{ step.stage }}</li></ol>
      <button v-if="busy && !querying" class="secondary-button" @click="track(job.id)">继续查询恢复任务</button>
    </div>
    <div v-if="error" class="notice error-notice" role="alert">{{ error }}</div>
    <div v-if="modal" class="modal-backdrop" @click.self="!busy && (modal = false)">
      <section class="modal-card recovery-modal" role="dialog" aria-modal="true" aria-labelledby="recovery-title">
        <button class="modal-close" :disabled="busy" aria-label="关闭设备恢复确认" @click="modal = false"><X :size="17" /></button>
        <h2 id="recovery-title">确认执行设备故障恢复</h2>
        <div class="impact-box"><strong>会中断后摩 NPU 上正在进行的请求</strong><span>影响 CoPaw、Qwen-VL、GLM-OCR、BGE、Reranker，以及同卡正在运行的 Qwen3.8 / CosyVoice。CPU 语音服务、CV 和网关管理页面不主动重启。</span></div>
        <p>恢复目标：{{ modeNames[confirmedMode] }} 独占模式。保存现存日志 → 停止同卡模型 → 单独加载目标模型 → 真实推理验证。</p>
        <p>遇到 fatal、加载超时或推理失败，立即停止继续加载并停止同卡模型；保留证据，不自动反复重试、不刷驱动。预计数分钟，异常时可能更久。</p>
        <label class="recovery-confirm">请输入“恢复设备”确认影响<input v-model="confirmation" aria-label="设备恢复确认文字" autocomplete="off" :disabled="busy" @keyup.enter="recover" /></label>
        <div class="modal-actions"><button class="secondary-button" :disabled="busy" @click="modal = false">取消</button><button class="primary-button" :disabled="busy || confirmation !== '恢复设备'" @click="recover">确认恢复设备</button></div>
      </section>
    </div>
  </section>
</template>

<style scoped>
.recovery-panel { padding: 18px; border-color: #e4cfa9; }
.recovery-heading { display: flex; align-items: center; gap: 12px; color: #906824; }
.recovery-heading h2 { color: #263e43; }
.recovery-heading p, .recovery-progress p { margin-top: 7px; font-size: 12px; line-height: 1.6; }
.recovery-action { margin-left: auto; flex-shrink: 0; }
.recovery-progress { margin-top: 16px; padding-top: 14px; border-top: 1px solid #e6eae8; font-size: 12px; }
.recovery-progress code { overflow-wrap: anywhere; }
.recovery-progress ol { padding-left: 22px; line-height: 1.8; }
.recovery-modal { width: min(560px, 100%); max-height: 90vh; overflow-y: auto; }
.recovery-modal > p { font-size: 13px; }
.recovery-confirm { display: block; margin: 18px 0; font-size: 13px; }
.recovery-confirm input { display: block; width: 100%; margin-top: 8px; padding: 10px; border: 1px solid #bfcfcd; border-radius: 7px; }
@media(max-width:760px) { .recovery-heading { flex-wrap: wrap; }.recovery-action { margin-left: 0; } }
</style>
