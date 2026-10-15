<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { Boxes, CircleCheck, LoaderCircle, RefreshCw, RotateCcw, Search, TriangleAlert } from '@lucide/vue'
import { describeError } from '../api'
import { models, overview, refreshOverview } from '../store'
import type { ModelRecord } from '../types'
import DeviceRecovery from './DeviceRecovery.vue'
import RuntimeSafetyNotice from '../RuntimeSafetyNotice.vue'
import { operations, operationState } from '../operations'

const query = ref('')
const statusFilter = ref('all')
const restarting = ref('')
const message = ref('')
const error = ref('')
const recovering = ref(false)

const serviceByModel: Record<string, string> = {
  'copaw-flash-9b': 'copaw', 'spark-x1-4b': 'spark-x1', 'qwen3-vl-8b': 'qwen-vl', 'qwen3.8-27b': 'qwen38', 'qwen3.6-35b-a3b': 'qwen36',
  'glm-ocr-0.9b': 'glm-ocr',
  'bge-m3': 'bge-m3', 'bge-reranker-v2-m3': 'reranker',
  'matcha-zh': 'matcha', 'kokoro-82m': 'kokoro', sensevoice: 'sensevoice',
  'cosyvoice3-0.5b': 'cosyvoice',
}

function serviceFor(model: ModelRecord) { return String(model.metadata?.service || serviceByModel[model.id] || (model.capability.startsWith('cv_') ? 'cv' : '')) }
function exclusive(model: ModelRecord) { return model.metadata?.exclusive === true || String(model.metadata?.device || '').includes('LQ50') }
const recoveryAvailable = computed(() => models.value.data.some(model => model.metadata?.recovery === true || String(model.metadata?.device || '').includes('LQ50')))
function restartHint(model: ModelRecord) {
  if (exclusive(model)) return '互斥模型请使用运行模式切换，避免与当前模型争用设备资源'
  return restartAllowed(model) ? `重启 ${model.id}` : '未激活、未部署或状态未确认，不能重启'
}
function restartAllowed(model: ModelRecord) {
  if (overview.value?.host.platform?.allow_restart === false) return false
  const container = overview.value?.host.containers.find(item => item.service === serviceFor(model))
  const mode = overview.value?.host.modelctl?.configured_mode
  return !!container && container.state !== 'absent' && model.enabled !== false && !['inactive','unavailable'].includes(model.status)
    && !model.safety
    && !exclusive(model)
    && (!model.modes?.length || (!!mode && model.modes.includes(mode)))
}

const capabilityLabels: Record<string, string> = {
  chat: '对话生成', embeddings: '向量嵌入', rerank: '文本重排',
  cv_classify: '图像分类', cv_detect: '目标检测', cv_face: '人脸分析', cv_segment: '图像分割',
  audio_asr: '语音识别', audio_tts: '语音合成', audio_vad: '语音活动检测', audio_speaker: '说话人表征',
  text_punctuation: '标点恢复', document_analysis: '文档分析', ocr: '文档识别',
}

const filtered = computed(() => models.value.data.filter(model => {
  const matched = !query.value || `${model.id} ${model.capability}`.toLowerCase().includes(query.value.toLowerCase())
  return matched && (statusFilter.value === 'all' || model.status === statusFilter.value)
}))

function latency(model: ModelRecord) {
  const value = overview.value?.gateway.models[model.id]?.latency_ms
  return value == null ? '—' : `${value.toFixed(0)} ms`
}

function statusText(value: string) {
  return ({ online: '在线', inactive: '未激活', unavailable: '未部署', error: '异常' } as Record<string, string>)[value] || value
}

async function restart(model: ModelRecord) {
  const service = serviceFor(model)
  if (!service || restarting.value || operationState.busy) return
  if (!restartAllowed(model)) {
    error.value = restartHint(model)
    return
  }
  if (!window.confirm(`确认重启 ${model.id}？重启期间该模型接口不可用。`)) return
  restarting.value = model.id
  error.value = ''
  message.value = ''
  try {
    await operations.submit(`/api/services/${service}/restart`, { method: 'POST' }, `重启 ${model.id}`)
    message.value = `${model.id} 已完成重启并通过健康检查；输出质量需另行验证。`
    await refreshOverview().catch(() => { message.value += ' 状态列表刷新失败，请稍后重新检查。' })
  } catch (reason) {
    error.value = describeError(reason)
  } finally {
    restarting.value = ''
  }
}

onMounted(() => { if (!overview.value) refreshOverview().catch(() => undefined) })
</script>

<template>
  <section class="page-body">
    <div v-if="message" class="notice success-notice"><CircleCheck :size="16" />{{ message }}</div>
    <div v-if="error" class="notice error-notice"><TriangleAlert :size="16" />{{ error }}</div>

    <DeviceRecovery v-if="recoveryAvailable" @busy="recovering = $event" />
    <RuntimeSafetyNotice />
    <div v-if="overview?.host.platform?.notice" class="notice">{{ overview.host.platform.notice }}</div>
    <section class="page-toolbar panel">
      <div class="search-box"><Search :size="16" /><input v-model="query" aria-label="搜索模型" placeholder="搜索模型 ID 或能力" /></div>
      <select v-model="statusFilter" aria-label="按状态筛选"><option value="all">全部状态</option><option value="online">在线</option><option value="inactive">未激活</option><option value="unavailable">未部署</option><option value="error">异常</option></select>
      <button class="secondary-button" @click="refreshOverview"><RefreshCw :size="15" />重新检查</button>
    </section>

    <section class="panel full-table-panel">
      <div class="panel-head">
        <div><h2>模型注册表</h2><p>共 {{ models.data.length }} 个模型，{{ overview?.gateway.online_models || 0 }} 个在线</p></div>
        <span class="subtle-code">mode: {{ overview?.gateway.active_mode || '—' }}</span>
      </div>
      <div class="table-wrap">
        <table class="management-table">
          <thead><tr><th>模型</th><th>能力</th><th>设备</th><th>运行模式</th><th>延迟</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            <tr v-for="model in filtered" :key="model.id">
              <td><div class="model-identity"><span><Boxes :size="16" /></span><div><strong>{{ model.id }}</strong><code>{{ model.backend || 'local' }}</code></div></div></td>
              <td>{{ capabilityLabels[model.capability] || model.capability }}<br /><RouterLink :to="{ path: '/capabilities', query: { model: model.id } }" class="model-capability-link">能力说明与示例</RouterLink></td>
              <td>{{ model.metadata?.device || '—' }}</td>
              <td><template v-if="model.modes?.length"><span v-for="mode in model.modes" :key="mode" class="tag">{{ mode }}</span></template><span v-else class="muted">常驻</span></td>
              <td class="mono">{{ latency(model) }}</td>
              <td><span :class="['status-pill', `status-${model.status}`]"><i></i>{{ model.safety?.reason === 'npu_quarantined' ? '已隔离' : statusText(model.status) }}</span><small v-if="model.safety" class="safety-reason">{{ model.safety.message }}</small></td>
              <td><button v-if="serviceFor(model)" class="icon-action" :disabled="operationState.busy || !!restarting || recovering || !restartAllowed(model)" :title="restartHint(model)" :aria-label="`重启 ${model.id}`" @click="restart(model)"><LoaderCircle v-if="restarting === model.id" class="spin" :size="15" /><RotateCcw v-else :size="15" />重启</button><span v-else class="muted">—</span></td>
            </tr>
            <tr v-if="!filtered.length"><td colspan="7" class="empty-row">没有符合条件的模型</td></tr>
          </tbody>
        </table>
      </div>
    </section>
  </section>
</template>

<style scoped>
.model-capability-link { display:inline-block; margin-top:6px; font-size:11px; color:#2e7a60; text-decoration:none; }
.safety-reason { display:block;max-width:230px;line-height:1.6;margin-top:7px;color:#9b453b;font-size:11px; }
</style>
