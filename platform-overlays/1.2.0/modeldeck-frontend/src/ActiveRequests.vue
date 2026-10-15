<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RefreshCw, Activity } from '@lucide/vue'
import { api, describeError } from './api'
import UsageRequests, { type UsageEvent } from './UsageRequests.vue'
import type { ResourcePolicy, ResourceSnapshot } from './keyPolicy'

interface Snapshot {
  observed_at: string; total: number; truncated: boolean; requests: UsageEvent[]
  in_flight: ResourceSnapshot['in_flight']; policy: ResourcePolicy
  models: { id: string; capability: string }[]
}
const emit = defineEmits<{ snapshot: [value: Snapshot] }>()
const snapshot = ref<Snapshot | null>(null), error = ref(''), loading = ref(false), automatic = ref(true)
let timer: ReturnType<typeof setInterval> | undefined, stopped = false
let controller: AbortController | undefined
const models = computed(() => {
  const value = snapshot.value
  if (!value) return []
  const ids = new Set([...Object.keys(value.in_flight.model), ...Object.keys(value.policy.model_limits).filter(k => value.policy.model_limits[k]! > 0)])
  return [...ids].map(id => ({ id, current:value.in_flight.model[id] || 0, limit:value.policy.model_limits[id] || 0 }))
})
async function refresh() {
  if (loading.value || stopped) return
  loading.value = true; controller = new AbortController()
  const timeout = setTimeout(() => controller?.abort(), 16000)
  try {
    const value = await api<Snapshot>('/api/requests/active', { signal:controller.signal })
    if (!value.in_flight || !Array.isArray(value.requests)) throw new Error('在途接口尚未就绪')
    if (!stopped) { snapshot.value = value; error.value = ''; emit('snapshot', value) }
  } catch (reason) { if (!stopped) error.value = describeError(reason) }
  finally { clearTimeout(timeout); loading.value = false }
}
function visibility() { if (!document.hidden && automatic.value) void refresh() }
onMounted(() => { void refresh(); timer = setInterval(() => { if (automatic.value && !document.hidden) void refresh() }, 5000); document.addEventListener('visibilitychange', visibility) })
onBeforeUnmount(() => { stopped = true; clearInterval(timer); controller?.abort(); document.removeEventListener('visibilitychange', visibility) })
</script>

<template>
  <section class="panel active-requests" aria-labelledby="active-requests-title">
    <div class="panel-head"><div><h2 id="active-requests-title"><Activity :size="19" />在途请求与并发占用</h2><p>查看正在占用名额的请求。后台任务和交互聊天共同使用模型额度。</p></div><div class="live-actions"><label><input v-model="automatic" type="checkbox" />每 5 秒刷新</label><button class="secondary-button" :disabled="loading" @click="refresh"><RefreshCw :class="{spin:loading}" :size="14" />刷新在途</button></div></div>
    <p v-if="error" role="alert" class="live-error">无法更新实时状态：{{ error }}。{{ snapshot ? '下方为上次成功快照，请勿当作实时数据。' : '当前占用未知。' }}</p>
    <div class="concurrency-cards">
      <article class="concurrency-card global"><small>网关在途总数</small><strong>{{ snapshot?.in_flight.global ?? '—' }}<span> / {{ snapshot ? (snapshot.policy.global_max_concurrent || '不设总上限') : '—' }}</span></strong><p>包括所有模型的已受理请求</p></article>
      <article v-for="model in models" :key="model.id" :class="['concurrency-card', {full:model.limit > 0 && model.current >= model.limit}]"><div class="model-heading"><code>{{ model.id }}</code><b>{{ model.limit > 0 && model.current >= model.limit ? '已满' : '有可用名额' }}</b></div><strong>{{ model.current }}<span> / {{ model.limit || '不设上限' }} 路</span></strong><div v-if="model.limit" class="capacity-track" role="progressbar" :aria-label="model.id + ' 并发占用'" :aria-valuenow="model.current" :aria-valuemin="0" :aria-valuemax="Math.max(model.limit, model.current)"><i :style="{width:Math.min(100,model.current/model.limit*100)+'%'}"></i></div><p>{{ model.limit ? `剩余 ${Math.max(0,model.limit-model.current)} 路` : '未设置单模型上限' }} · 新请求还受 Key、设备组和总额度约束</p></article>
    </div>
    <div class="live-caption"><span>{{ snapshot ? `快照时间：${new Date(snapshot.observed_at).toLocaleTimeString('zh-CN', {hour12:false})}` : '正在读取实时状态…' }}</span><span v-if="!automatic">自动刷新已暂停</span><span>仅统计经当前网关受理且尚未释放的请求，不等于引擎实际运行数。</span></div>
    <p v-if="snapshot?.truncated" class="live-error">共 {{ snapshot.total }} 条在途请求，下方仅显示最早的 {{ snapshot.requests.length }} 条。</p>
    <UsageRequests v-if="snapshot" :events="snapshot.requests" :empty-text="error ? '上次快照没有在途请求，当前占用未知。' : '当前没有在途请求，并发名额已空闲。'" @cancelled="refresh" />
  </section>
</template>

<style scoped>
.active-requests{overflow:hidden}.panel-head h2{display:flex;align-items:center;gap:8px}.live-actions{display:flex;align-items:center;gap:15px;flex-wrap:wrap}.live-actions label{font-size:12px;display:flex;align-items:center;gap:6px;color:#59716e}.concurrency-cards{display:flex;gap:14px;flex-wrap:wrap;padding:2px 20px 16px}.concurrency-card{flex:1;min-width:260px;border:1px solid #cde4da;border-radius:12px;padding:18px;background:#f3faf6}.concurrency-card.global{flex:0 1 260px;background:#f5f7f8;border-color:#e0e6e8}.concurrency-card small{font-size:12px;color:#687d78}.concurrency-card strong{display:block;margin:12px 0;font-size:30px;color:#185b47}.concurrency-card strong span{font-size:15px;font-weight:500;color:#61766e}.model-heading{display:flex;gap:10px;align-items:center;justify-content:space-between;font-size:13px}.model-heading code{overflow-wrap:anywhere}.model-heading b{font-size:11px;white-space:nowrap;color:#18734f}.concurrency-card p{font-size:11px;color:#687e76;line-height:1.7;margin:10px 0 0}.capacity-track{height:6px;border-radius:8px;background:#dbe9e1;overflow:hidden}.capacity-track i{display:block;height:100%;background:#278760}.concurrency-card.full{background:#fff8ec;border-color:#efd6a8}.full .model-heading b,.full strong{color:#946115}.full .capacity-track i{background:#d79532}.live-caption{padding:0 20px 14px;display:flex;flex-wrap:wrap;gap:8px 18px;font-size:11px;color:#72817e}.live-error{margin:0 20px 16px;padding:10px;background:#fff1e9;color:#a24d28;font-size:12px;line-height:1.6}.no-active{text-align:center;padding:24px;color:#687e76;font-size:13px;border-top:1px solid #edf0ee}@media(max-width:700px){.panel-head{flex-wrap:wrap;gap:15px}.concurrency-card,.concurrency-card.global{flex:1 1 100%;min-width:0}.live-actions{width:100%;justify-content:space-between}}
</style>
