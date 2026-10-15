<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { CircleCheck, Gauge, LoaderCircle, RotateCcw, Save, SlidersHorizontal, Sparkles, TriangleAlert } from '@lucide/vue'
import { api, describeError } from '../api'
import type { ConfigField, ConfigModel, ConfigResponse, ConfigValidation, RuntimeSettings, RuntimeSettingValue } from '../types'
import { operations, operationState } from '../operations'

const config = ref<ConfigResponse | null>(null)
const draftModels = ref<ConfigModel[]>([])
const draftSettings = ref<RuntimeSettings>({})
const loading = ref(true)
const validating = ref(false)
const applying = ref(false)
const error = ref('')
const message = ref('')
const validation = ref<ConfigValidation | null>(null)
const selectedGroup = ref('gateway')
const selectedProfile = ref('stable')

const dirty = computed(() => config.value ? (
  JSON.stringify(config.value.settings) !== JSON.stringify(draftSettings.value) ||
  JSON.stringify(config.value.models) !== JSON.stringify(draftModels.value)
) : false)
const groups = computed(() => config.value?.schema.groups || [])
const profiles = computed(() => config.value?.schema.profiles || [])
const activeGroup = computed(() => groups.value.find(group => group.id === selectedGroup.value))

const serviceLabels: Record<string, string> = {
  gateway: '模型网关',
  sensevoice: 'SenseVoice',
}

function cloneData<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const response = await api<ConfigResponse>('/api/config')
    config.value = response
    draftModels.value = cloneData(response.models)
    draftSettings.value = cloneData(response.settings)
    selectedGroup.value = response.schema.groups[0]?.id || 'gateway'
    validation.value = null
  } catch (reason) {
    error.value = describeError(reason)
  } finally {
    loading.value = false
  }
}

function payload() {
  return {
    settings: draftSettings.value,
    models: draftModels.value.map(({ id, enabled, base_url }) => ({ id, enabled, base_url })),
  }
}

function fieldDisabled(field: ConfigField) {
  if (field.depends_on && !draftSettings.value[field.depends_on]) return true
  if (field.depends_not) return Object.entries(field.depends_not).some(([key, value]) => draftSettings.value[key] === value)
  return false
}

function applyProfile() {
  const profile = profiles.value.find(item => item.id === selectedProfile.value)
  if (!profile) return
  draftSettings.value = { ...draftSettings.value, ...cloneData(profile.values) }
  validation.value = null
  message.value = `已载入“${profile.label}”，请校验后再应用。`
  error.value = ''
}

async function validate() {
  validating.value = true
  error.value = ''
  message.value = ''
  try {
    validation.value = await api<ConfigValidation>('/api/config/validate', { method: 'POST', body: JSON.stringify(payload()) })
    message.value = validation.value.changes.length
      ? `校验通过，共 ${validation.value.changes.length} 项配置将发生变化。`
      : '校验通过，当前没有待应用的变化。'
  } catch (reason) {
    validation.value = null
    error.value = describeError(reason)
  } finally {
    validating.value = false
  }
}

async function apply() {
  if (!dirty.value || applying.value || operationState.busy || !validation.value?.changes.length) return
  applying.value = true
  error.value = ''
  message.value = '正在备份配置、重建受影响服务并执行健康检查…'
  try {
    validation.value = await api<ConfigValidation>('/api/config/validate', { method: 'POST', body: JSON.stringify(payload()) })
    if (!validation.value.changes.length) { message.value = '没有需要应用的变化。'; return }
    await operations.submit('/api/config/apply', { method: 'POST', body: JSON.stringify(payload()) }, '应用运行配置')
    await load()
    message.value = '配置已应用，受影响服务健康检查通过，备份版本已保留。'
  } catch (reason) {
    error.value = describeError(reason)
    message.value = ''
  } finally {
    applying.value = false
  }
}

function reset() {
  if (!config.value) return
  draftModels.value = cloneData(config.value.models)
  draftSettings.value = cloneData(config.value.settings)
  validation.value = null
  message.value = ''
  error.value = ''
}

function setValue(id: string, value: RuntimeSettingValue) {
  draftSettings.value[id] = value
  validation.value = null
}

function displayValue(value: unknown) {
  if (value === true) return '开启'
  if (value === false) return '关闭'
  if (value === null || value === undefined || value === '') return '空'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

onMounted(load)
</script>

<template>
  <section class="page-body config-workspace">
    <div class="config-warning"><ShieldCheck :size="18" /><div><strong>面向交付人员的安全配置</strong><span>无需修改代码。所有字段都有范围校验，应用前显示影响，失败时自动恢复备份。</span></div></div>
    <div v-if="message" class="notice success-notice"><CircleCheck :size="16" />{{ message }}</div>
    <div v-if="error" class="notice error-notice"><TriangleAlert :size="16" />{{ error }}</div>
    <div v-if="loading" class="panel loading-block"><LoaderCircle class="spin" :size="20" />正在读取运行配置…</div>

    <template v-else-if="config">
      <section class="panel config-profile-panel">
        <div class="profile-copy"><span class="profile-icon"><Sparkles :size="20" /></span><div><h2>按使用场景配置</h2><p>预设只填写推荐值，不会立即应用；你仍可逐项检查和修改。</p></div></div>
        <div class="profile-actions">
          <label>配置预设<select v-model="selectedProfile" data-testid="config-profile"><option v-for="profile in profiles" :key="profile.id" :value="profile.id">{{ profile.label }}</option></select></label>
          <button class="secondary-button" data-testid="apply-profile" @click="applyProfile"><SlidersHorizontal :size="15" />载入预设</button>
        </div>
      </section>


      <nav class="config-tabs" aria-label="配置分类">
        <button v-for="group in groups" :key="group.id" :class="{ active: selectedGroup === group.id }" :data-testid="`tab-${group.id}`" @click="selectedGroup = group.id">{{ group.label }}</button>
        <button :class="{ active: selectedGroup === 'models' }" data-testid="tab-models" @click="selectedGroup = 'models'">模型与上游</button>
      </nav>

      <section v-if="activeGroup && selectedGroup !== 'models'" class="panel settings-panel">
        <div class="panel-head"><div><h2>{{ activeGroup.label }}</h2><p>{{ activeGroup.description }}</p></div><span class="scope-chip"><Gauge :size="14" />自动判断重启范围</span></div>
        <div class="settings-grid">
          <label v-for="field in activeGroup.fields" :key="field.id" :class="['setting-field', { disabled: fieldDisabled(field), risky: field.risk === 'high' }]">
            <span class="setting-copy"><strong>{{ field.label }}</strong><small>{{ field.help || '修改后先校验再应用。' }}</small></span>
            <span v-if="field.type === 'boolean'" class="setting-switch">
              <input :checked="Boolean(draftSettings[field.id])" :disabled="fieldDisabled(field)" :data-testid="`setting-${field.id}`" type="checkbox" @change="setValue(field.id, ($event.target as HTMLInputElement).checked)" /><i></i><em>{{ draftSettings[field.id] ? '开启' : '关闭' }}</em>
            </span>
            <span v-else-if="field.type === 'select'" class="setting-input">
              <select :value="String(draftSettings[field.id])" :disabled="fieldDisabled(field)" :data-testid="`setting-${field.id}`" @change="setValue(field.id, ($event.target as HTMLSelectElement).value)"><option v-for="choice in field.choices" :key="choice.value" :value="choice.value">{{ choice.label }}</option></select>
            </span>
            <span v-else class="setting-input number-input">
              <input :value="Number(draftSettings[field.id])" :min="field.min" :max="field.max" :step="field.step || 1" :disabled="fieldDisabled(field)" :data-testid="`setting-${field.id}`" type="number" @input="setValue(field.id, Number(($event.target as HTMLInputElement).value))" /><em v-if="field.unit">{{ field.unit }}</em>
            </span>
          </label>
        </div>
      </section>

      <section v-else-if="selectedGroup === 'models'" class="panel config-panel">
        <div class="panel-head"><div><h2>模型与上游</h2><p>常用操作使用开关；内部服务地址收纳在高级设置中。</p></div></div>
        <div class="config-list">
          <article v-for="item in draftModels" :key="item.id" :class="['config-row', 'expanded-config-row', { disabled: !item.enabled }]">
            <div class="config-identity"><strong>{{ item.id }}</strong><span>{{ item.capability }}<template v-if="item.modes.length"> · {{ item.modes.join(' / ') }}</template></span></div>
            <label class="compact-switch"><span>启用模型</span><input v-model="item.enabled" type="checkbox" @change="validation = null" /><i></i></label>
            <details class="model-advanced"><summary>高级设置</summary><label class="url-field">内部上游地址<input v-model.trim="item.base_url" type="url" spellcheck="false" @input="validation = null" /></label><small>仅在更换内部服务地址时修改，不可填写用户名或密码。</small></details>
          </article>
        </div>
      </section>

      <section v-if="validation?.changes.length" class="panel impact-panel" data-testid="impact-preview">
        <div class="panel-head"><div><h2>变更影响预览</h2><p>确认影响范围后再应用，系统会自动备份当前版本。</p></div><span class="change-count">{{ validation.changes.length }} 项变化</span></div>
        <div class="impact-summary">
          <div><strong>受影响服务</strong><span>{{ validation.affected_services.map(item => serviceLabels[item] || item).join('、') || '无' }}</span></div>
          <div><strong>预计接口中断</strong><span>不超过 {{ validation.estimated_downtime_seconds }} 秒</span></div>
          <div><strong>失败处理</strong><span>自动恢复全部配置并重新健康检查</span></div>
        </div>
        <div class="change-list readable-change-list"><div v-for="change in validation.changes" :key="String(change.id)"><strong>{{ change.id }}</strong><code>{{ displayValue(change.before) }}</code><span>→</span><code>{{ displayValue(change.after) }}</code></div></div>
      </section>

      <section class="panel config-apply-bar">
        <div><strong>{{ dirty ? '存在未应用更改' : '当前配置已保存' }}</strong><span>{{ validation?.changes.length ? '已校验，可以确认应用。' : '修改字段后先执行校验。' }}</span></div>
        <div class="config-actions">
          <button class="secondary-button" :disabled="!dirty || applying" @click="reset"><RotateCcw :size="15" />恢复当前值</button>
          <button class="secondary-button" data-testid="validate-config" :disabled="!dirty || validating || applying" @click="validate"><LoaderCircle v-if="validating" class="spin" :size="15" /><CircleCheck v-else :size="15" />校验并预览</button>
          <button class="primary-button" data-testid="apply-config" :disabled="operationState.busy || !dirty || applying || !validation?.changes.length" @click="apply"><LoaderCircle v-if="applying" class="spin" :size="15" /><Save v-else :size="15" />确认应用</button>
        </div>
      </section>
    </template>
  </section>
</template>
