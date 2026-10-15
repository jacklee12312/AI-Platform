<script setup lang="ts">
import { computed } from 'vue'
import type { KeyPolicy, PolicyModel } from './keyPolicy'
const policy = defineModel<KeyPolicy>({ required: true })
defineProps<{ models: PolicyModel[]; defaultRpm: number; disabled?: boolean }>()
const localExpiry = computed({
  get() {
    if (!policy.value.expires_at) return ''
    const date = new Date(policy.value.expires_at)
    return new Date(date.getTime() - date.getTimezoneOffset() * 60_000).toISOString().slice(0, 16)
  },
  set(value: string) { policy.value.expires_at = value ? new Date(value).toISOString() : null },
})
function selectModel(id: string, checked: boolean) {
  const selected = new Set(policy.value.allowed_models || [])
  if (checked) selected.add(id); else selected.delete(id)
  policy.value.allowed_models = [...selected]
}
</script>

<template>
  <fieldset class="key-policy-fields" :disabled="disabled">
    <div class="policy-form-grid">
      <label><span>应用 / 部门归属</span><input v-model="policy.application" maxlength="64" placeholder="例如：材料审核系统" /></label>
      <label><span>到期时间（本机时区，留空永久有效）</span><input v-model="localExpiry" type="datetime-local" /><button v-if="localExpiry" type="button" class="clear-expiry" @click="localExpiry = ''">清除到期时间</button></label>
      <label><span>每分钟请求上限</span><input v-model.number="policy.rate_limit_per_minute" type="number" min="0" max="1000000000" step="1" /><small>0 继承网关默认 {{ defaultRpm }} 次，含模型列表查询。</small></label>
      <label><span>Key 同时在途请求上限</span><input v-model.number="policy.max_concurrent_requests" type="number" min="0" max="1024" step="1" /><small>0 不单独限制；流式连接持续占用名额。</small></label>
      <label><span>每日推理请求额度</span><input v-model.number="policy.daily_request_limit" type="number" min="0" max="1000000000" step="1" /><small>0 不限制。北京时间 00:00 重置，受理后即计次，失败或取消也计入。</small></label>
      <label><span>每月已知 Token 预警线</span><input v-model.number="policy.monthly_token_warning" type="number" min="0" max="1000000000" step="1" /><small>0 不预警。仅提示，不拦截；缺失用量仍保持未知。</small></label>
    </div>
    <div class="model-permission">
      <label><span>允许调用的模型</span><select :value="policy.allowed_models === null ? 'all' : 'selected'" @change="policy.allowed_models = ($event.target as HTMLSelectElement).value === 'all' ? null : []"><option value="all">全部模型（包括以后新增的模型）</option><option value="selected">仅允许勾选的模型</option></select></label>
      <template v-if="policy.allowed_models !== null">
        <p class="selection-summary">已选 {{ policy.allowed_models.length }} 个；不勾选任何模型将禁止全部推理调用。</p>
        <div class="model-checkboxes">
          <label v-for="model in models" :key="model.id"><input type="checkbox" :checked="policy.allowed_models.includes(model.id)" @change="selectModel(model.id, ($event.target as HTMLInputElement).checked)" /><span><strong>{{ model.id }}</strong><small>{{ model.capability }} · {{ model.modes.join(' / ') || '所有模式' }}</small></span></label>
        </div>
        <p v-if="!models.length" class="selection-summary">模型列表尚未加载，请刷新后再设置。</p>
      </template>
      <p class="selection-summary">授权不会启动模型；模型仍需已部署且处于可用模式。内部 Guard 属于聊天处理流程。</p>
    </div>
  </fieldset>
</template>

<style scoped>
.key-policy-fields { border:0; padding:0; margin:16px 0; min-width:0; text-align:left; }
.policy-form-grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
label > span { display:block; font-size:12px; font-weight:650; color:#405a62; margin-bottom:6px; }
input:not([type=checkbox]), select { width:100%; min-height:38px; border:1px solid #cfdcde; border-radius:7px; padding:7px 10px; background:white; color:#29454d; font-size:12px; }
small { display:block; color:#71868b; font-size:11px; line-height:1.6; margin-top:5px; }
.model-permission { margin-top:20px; border-top:1px solid #e0e8e8; padding-top:16px; }
.selection-summary { font-size:12px; color:#71868b; margin:8px 0; line-height:1.6; }
.model-checkboxes { display:grid; grid-template-columns:1fr 1fr; gap:8px; max-height:230px; overflow:auto; padding:4px; }
.model-checkboxes label { display:flex; align-items:flex-start; gap:8px; padding:8px; border:1px solid #dce6e5; border-radius:7px; }
.model-checkboxes input { margin-top:4px; accent-color:#267861; flex-shrink:0; }
.model-checkboxes strong { font-size:11px; overflow-wrap:anywhere; }.model-checkboxes small { font-weight:normal; }
.clear-expiry { border:0; background:none; color:#267861; font-size:11px; padding:4px 0; cursor:pointer; }
@media(max-width:600px) { .policy-form-grid,.model-checkboxes { grid-template-columns:1fr; } }
</style>
