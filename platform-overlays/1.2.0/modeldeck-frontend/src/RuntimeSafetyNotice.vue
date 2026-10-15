<script setup lang="ts">
import { computed } from 'vue'
import { ShieldAlert } from '@lucide/vue'
import { overview } from './store'

const affected = computed(() => Object.entries(overview.value?.gateway.models || {}).filter(([, value]) => value.reason?.startsWith('npu_')))
const incident = computed(() => affected.value.find(([, value]) => value.incident)?.[1].incident)
const recovering = computed(() => affected.value.some(([, value]) => value.reason === 'npu_recovering'))
const startup = computed(() => overview.value?.host.startup_verification)
const startupActive = computed(() => startup.value?.current_boot && ['waiting', 'queued', 'running'].includes(startup.value.status))
const startupFailed = computed(() => startup.value?.current_boot && startup.value.status === 'failed')
const percent = computed(() => startup.value?.total ? Math.round((startup.value.completed || 0) / startup.value.total * 100) : 0)
</script>

<template>
  <section v-if="affected.length" class="runtime-safety-notice" role="alert" aria-label="NPU 请求隔离状态">
    <ShieldAlert :size="23" />
    <div>
      <h3>{{ incident ? 'NPU 故障已隔离' : startupActive ? '开机后正在验证 NPU 模型' : startupFailed ? '开机自动验证失败' : recovering ? 'NPU 正在恢复与验证' : 'NPU 暂不接收新请求' }}</h3>
      <p>{{ affected[0]?.[1].message }}。影响 {{ affected.length }} 个模型。</p>
      <template v-if="startupActive">
        <p>{{ startup?.stage || '正在准备真实推理验证' }}<template v-if="startup?.current_model"> · 当前模型 <code>{{ startup.current_model }}</code></template></p>
        <progress v-if="startup?.total" :value="startup.completed || 0" :max="startup.total"></progress>
        <small v-if="startup?.total">已验证 {{ startup.completed || 0 }} / {{ startup.total }}（{{ percent }}%）<template v-if="startup.elapsed_seconds != null"> · {{ Math.floor(startup.elapsed_seconds) }} 秒</template></small>
      </template>
      <p v-if="startupFailed" class="startup-error">{{ startup?.error || startup?.stage }}</p>
      <p v-if="incident">首次故障：{{ new Date(incident.at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) }}（北京时间）· {{ incident.source }} · {{ incident.kind }}</p>
      <details v-if="incident"><summary>查看首个故障证据</summary><p>{{ incident.detail }}</p><small>故障编号：{{ incident.id }}</small></details>
      <p v-if="startupActive">全部目标模型通过后会自动开放网关，请勿重复提交恢复或切换任务。</p>
      <p v-else>Docker 显示 Healthy 不会解除隔离。请在模型管理中执行设备故障恢复，完成当前实例的实际推理验证。</p>
    </div>
  </section>
</template>

<style scoped>
.runtime-safety-notice{display:flex;gap:12px;padding:18px;border:1px solid #e5b6aa;border-radius:10px;background:#fff4ee;color:#873e31;min-width:0}.runtime-safety-notice>svg{flex-shrink:0}.runtime-safety-notice>div{min-width:0}.runtime-safety-notice h3{font-size:15px}.runtime-safety-notice p{font-size:12px;line-height:1.8;margin-top:8px;overflow-wrap:anywhere}.runtime-safety-notice details{margin-top:10px;font-size:12px}.runtime-safety-notice summary{cursor:pointer}.runtime-safety-notice small{display:block;margin-top:7px;overflow-wrap:anywhere}.runtime-safety-notice code{font-size:11px}.runtime-safety-notice progress{display:block;width:min(520px,100%);height:8px;margin-top:10px;accent-color:#b86a3f}.runtime-safety-notice .startup-error{font-weight:650}
</style>
