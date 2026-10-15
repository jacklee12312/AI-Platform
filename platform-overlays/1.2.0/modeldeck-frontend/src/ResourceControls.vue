<script setup lang="ts">
import { computed, ref } from 'vue'
import { api, describeError } from './api'
import { rejectionLabel, type ResourcePolicy, type ResourceSnapshot } from './keyPolicy'
const props = defineProps<{ snapshot: ResourceSnapshot | null }>()
const emit = defineEmits<{ refresh: [] }>()
const editing = ref(false), saving = ref(false), error = ref(''), message = ref('')
const draft = ref<ResourcePolicy>({ global_max_concurrent: 0, pool_limits: {}, model_limits: {} })
const pools = computed(() => [...new Set([
  ...(props.snapshot?.models.flatMap(model => model.pools) || []),
  ...Object.keys(props.snapshot?.policy.pool_limits || {}),
])])
const configuredModels = computed(() => Object.values(props.snapshot?.policy.model_limits || {}).filter(v => v > 0).length)
function edit() {
  if (!props.snapshot) return
  draft.value = JSON.parse(JSON.stringify(props.snapshot.policy))
  for (const pool of pools.value) draft.value.pool_limits[pool] ??= 0
  for (const model of props.snapshot.models) draft.value.model_limits[model.id] ??= 0
  error.value = ''; message.value = ''; editing.value = true
}
async function save() {
  if (saving.value) return
  saving.value = true; error.value = ''
  try {
    const value = JSON.parse(JSON.stringify(draft.value)) as ResourcePolicy
    const number = (raw: unknown) => { const n = Number(raw); if (!Number.isInteger(n) || n < 0 || n > 1024) throw new Error('并发上限必须为 0–1024 的整数'); return n }
    value.global_max_concurrent = number(value.global_max_concurrent)
    for (const map of [value.pool_limits, value.model_limits]) for (const key of Object.keys(map)) map[key] = number(map[key])
    await api('/api/resource-policy', { method: 'PUT', body: JSON.stringify(value) })
    editing.value = false; message.value = '并发策略已保存，对后续新请求生效。'; emit('refresh')
  } catch (reason) { error.value = describeError(reason) } finally { saving.value = false }
}
</script>

<template>
  <section class="panel resource-panel">
    <div class="panel-head"><div><h2>网关资源控制</h2><p>限制同时在途的推理请求。达到任一上限返回 429；管理台推理测试也占用资源。</p></div><button class="secondary-button" :disabled="!snapshot || saving" @click="edit">配置并发上限</button></div>
    <div v-if="message" class="resource-message" role="status">{{ message }}</div>
    <div class="resource-summary"><div><small>当前网关在途</small><strong>{{ snapshot?.in_flight.global ?? '—' }} / {{ snapshot?.policy.global_max_concurrent || '不限' }}</strong></div><div v-for="pool in pools" :key="pool"><small>{{ pool }} 设备组</small><strong>{{ snapshot?.in_flight.pool[pool] || 0 }} / {{ snapshot?.policy.pool_limits[pool] || '不限' }}</strong></div><div><small>已设模型并发上限</small><strong>{{ configuredModels }} 个</strong></div></div>
    <p class="resource-help">数字随页面刷新更新。0 表示不额外限制；降低上限不会中断已有请求。规则仅对经网关的调用生效，控制当前网关的在途请求数；直接访问模型服务端口不受这些规则限制。多项限制同时生效。</p>
    <details class="rejection-list"><summary>最近的权限与资源拒绝（{{ snapshot?.recent_rejections.length || 0 }} 条）</summary><div class="table-wrap"><table><thead><tr><th>时间</th><th>Key</th><th>模型</th><th>原因</th><th>Request ID</th></tr></thead><tbody><tr v-if="!snapshot?.recent_rejections.length"><td colspan="5">暂无拒绝记录</td></tr><tr v-for="row in snapshot?.recent_rejections" :key="row.sequence"><td>{{ new Date(row.created_at).toLocaleString() }}</td><td>{{ row.key_name }}</td><td>{{ row.model }}</td><td>{{ rejectionLabel(row.code) }}</td><td><code>{{ row.request_id }}</code></td></tr></tbody></table></div></details>
    <div v-if="editing" class="modal-backdrop" @click.self="!saving && (editing = false)"><form class="modal-card resource-dialog" role="dialog" aria-modal="true" aria-label="配置并发上限" @submit.prevent="save"><h2>配置并发上限</h2><p>保存后即时生效，无需重启。0 表示不限制；超过上限的新请求直接拒绝。</p><p v-if="error" role="alert" class="error-text">{{ error }}</p><fieldset :disabled="saving"><div class="limits-grid"><label>网关总并发<input v-model.number="draft.global_max_concurrent" type="number" min="0" max="1024" /></label><label v-for="pool in pools" :key="pool">{{ pool }} 设备组<input v-model.number="draft.pool_limits[pool]" type="number" min="0" max="1024" /></label></div><h3>单模型并发</h3><div class="model-limits"><label v-for="model in snapshot?.models" :key="model.id"><span>{{ model.id }}<small>当前在途 {{ snapshot?.in_flight.model[model.id] || 0 }} · {{ model.pools.join(' / ') || '未分组' }}</small></span><input v-model.number="draft.model_limits[model.id]" :aria-label="model.id + ' 并发上限'" type="number" min="0" max="1024" /></label></div></fieldset><div class="modal-actions"><button type="button" class="secondary-button" :disabled="saving" @click="editing = false">取消</button><button class="primary-button" :disabled="saving">{{ saving ? '正在保存…' : '保存并发策略' }}</button></div></form></div>
  </section>
</template>

<style scoped>
.resource-summary { display:flex; flex-wrap:wrap; gap:24px; padding:16px 20px; }.resource-summary small { display:block; font-size:12px; color:#71868b; }.resource-summary strong { display:block; margin-top:6px; font-size:18px; color:#28584e; }
.resource-help,.resource-message { margin:0 20px 16px; font-size:12px; line-height:1.7; color:#61777e; }.resource-message { color:#267861; }
.rejection-list { padding:0 20px 18px; font-size:12px; }.rejection-list summary { cursor:pointer; margin-bottom:12px; }.rejection-list code { font-size:10px; overflow-wrap:anywhere; }
.resource-dialog { width:min(760px,calc(100vw - 32px)); max-height:90vh; overflow:auto; text-align:left; }.resource-dialog > p { font-size:12px; color:#71868b; line-height:1.6; }
fieldset { border:0; padding:0; margin:16px 0; min-width:0; }.limits-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; }label { font-size:12px; color:#405a62; }input { display:block; width:100%; margin-top:6px; border:1px solid #cfdcde; border-radius:7px; padding:8px; font-size:12px; }
h3 { margin:20px 0 10px; font-size:14px; }.model-limits { max-height:300px; overflow:auto; }.model-limits label { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:8px 2px; border-bottom:1px solid #edf0f0; }.model-limits span { overflow-wrap:anywhere; }.model-limits input { width:86px; flex-shrink:0; }.model-limits small { display:block; color:#71868b; font-size:11px; margin-top:4px; }.error-text { color:#a14343!important; }
@media(max-width:600px) { .limits-grid { grid-template-columns:1fr; }.panel-head { align-items:flex-start; gap:12px; flex-direction:column; } }
</style>
