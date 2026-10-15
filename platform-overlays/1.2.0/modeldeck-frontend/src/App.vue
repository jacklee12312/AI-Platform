<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import {
  Boxes,
  Braces,
  Download,
  BookOpen,
  ScanLine,
  ClipboardCheck,
  ChevronDown,
  FlaskConical,
  Gauge,
  KeyRound,
  Files,
  LayoutDashboard,
  LoaderCircle,
  LogOut,
  RefreshCw,
  ScrollText,
  SlidersHorizontal,
} from '@lucide/vue'
import { api, setCsrf } from './api'
import { loadingOverview, overview, refreshOverview } from './store'
import LoginView from './views/LoginView.vue'
import OperationStatus from './OperationStatus.vue'

const route = useRoute()
const state = ref<'checking' | 'authenticated' | 'anonymous'>('checking')
const menuOpen = ref(false)
const uiBuild = import.meta.env.VITE_UI_BUILD
const updateAvailable = ref(false)
const features = ref(new Set(['all']))
let startupRefreshTimer: ReturnType<typeof setInterval> | undefined
let buildRefreshTimer: ReturnType<typeof setInterval> | undefined

async function checkUiBuild() {
  if (!import.meta.env.PROD) return
  try {
    const response = await fetch('/build.json', { cache: 'no-store', credentials: 'same-origin' })
    if (!response.ok) return
    const value = await response.json()
    updateAvailable.value = typeof value.build === 'string' && value.build !== uiBuild
  } catch { /* Offline status is handled by the normal status refresh. */ }
}
function reloadPage() { window.location.reload() }
function refreshStatus() {
  checkUiBuild()
  refreshOverview().catch(() => undefined)
}

const navGroups = [
  { label: '设备运维', items: [
    { label: '设备总览', icon: LayoutDashboard, to: '/', feature: 'dashboard' },
    { label: '组件中心', icon: Boxes, to: '/components', feature: 'components' },
    { label: '任务中心', icon: Files, to: '/tasks', feature: 'tasks' },
    { label: '模型体验', icon: FlaskConical, to: '/test', feature: 'test' },
    { label: '批量任务', icon: Files, to: '/batches', feature: 'batches' },
  ]},
  { label: '接入管理', items: [
    { label: '能力与示例', icon: BookOpen, to: '/capabilities', feature: 'capabilities' },
    { label: 'API 参考', icon: Braces, to: '/api-reference', feature: 'api-reference' },
    { label: 'Key 与用量', icon: KeyRound, to: '/usage', feature: 'usage' },
  ]},
  { label: '模型运维', items: [
    { label: '模型中心', icon: Boxes, to: '/models', feature: 'models' },
    { label: '运行与硬件', icon: Download, to: '/runtime', feature: 'models' },
    { label: '配置管理', icon: SlidersHorizontal, to: '/config', feature: 'config' },
  ]},
  { label: '系统交付', items: [
    { label: '自检与评测', icon: ClipboardCheck, to: '/evaluations', feature: 'evaluations' },
    { label: '运行诊断', icon: ScanLine, to: '/diagnostics', feature: 'diagnostics' },
    { label: '日志审计', icon: ScrollText, to: '/logs', feature: 'logs' },
  ]},
]
const visibleNavGroups = computed(() => navGroups.map(group => ({
  ...group,
  items: group.items.filter(item => features.value.has('all') || features.value.has(item.feature)),
})).filter(group => group.items.length))

const pageTitle = computed(() => String(route.meta.title || '运行总览'))
const pageEnabled = computed(() => (route.path === '/model-install' && features.value.has('model-install')) || features.value.has('all') || navGroups.some(group => group.items.some(item => item.to === route.path && features.value.has(item.feature))))
const online = computed(() => overview.value?.gateway.status === 'ok')

async function checkSession() {
  try {
    const value = await api<{ authenticated: boolean; csrf?: string; features?: string[] }>('/api/session')
    features.value = new Set(value.features?.length ? value.features : ['all'])
    if (!value.authenticated || !value.csrf) {
      state.value = 'anonymous'
      return
    }
    setCsrf(value.csrf)
    state.value = 'authenticated'
    refreshOverview().catch(() => undefined)
  } catch {
    state.value = 'anonymous'
  }
}

function authenticated() { checkSession() }

async function logout() {
  try { await api('/api/logout', { method: 'POST' }) } catch { /* local session is cleared below */ }
  setCsrf('')
  state.value = 'anonymous'
  menuOpen.value = false
}

onMounted(() => {
  checkSession()
  checkUiBuild()
  window.addEventListener('focus', checkUiBuild)
  buildRefreshTimer = setInterval(checkUiBuild, 60000)
  startupRefreshTimer = setInterval(() => {
    const startup = overview.value?.host.startup_verification
    if (state.value === 'authenticated' && startup?.current_boot && ['waiting', 'queued', 'running'].includes(startup.status) && !loadingOverview.value) {
      refreshOverview().catch(() => undefined)
    }
  }, 2000)
})
onUnmounted(() => {
  if (startupRefreshTimer) clearInterval(startupRefreshTimer)
  if (buildRefreshTimer) clearInterval(buildRefreshTimer)
  window.removeEventListener('focus', checkUiBuild)
})
</script>

<template>
  <div v-if="state === 'checking'" class="boot-screen"><div class="brand-mark"><Gauge :size="22" /></div><LoaderCircle class="spin" :size="20" /><span>正在连接本地管理服务</span></div>
  <LoginView v-else-if="state === 'anonymous'" @authenticated="authenticated" />
  <div v-else class="app-shell">
    <aside class="sidebar">
      <div class="brand"><div class="brand-mark"><Gauge :size="21" /></div><div><strong>ModelDeck</strong><span>单机运维控制台 · v1.2.0</span></div></div>
      <nav class="nav-list" aria-label="主导航">
        <section v-for="group in visibleNavGroups" :key="group.label" class="nav-group">
          <span class="nav-group-label">{{ group.label }}</span>
          <RouterLink v-for="item in group.items" :key="item.to" :to="item.to" class="nav-item">
            <component :is="item.icon" :size="18" stroke-width="1.8" /><span>{{ item.label }}</span>
          </RouterLink>
        </section>
      </nav>
      <div class="sidebar-foot">
        <div class="cluster-state"><span :class="['pulse', !online && 'offline-pulse']"></span><div><strong>{{ online ? '集群运行正常' : '集群状态待检查' }}</strong><small>{{ overview?.gateway.online_models || 0 }} 个模型在线</small></div></div>
        <div class="version-line"><span>Gateway</span><code>v{{ overview?.gateway.version || '—' }}</code></div>
        <div class="version-line" :title="uiBuild"><span>UI 构建</span><code>{{ uiBuild?.replace('T', ' ').slice(0, 16) }} UTC</code></div>
      </div>
    </aside>

    <main class="main-panel">
      <header class="topbar">
        <div><p class="eyebrow">{{ overview?.host.platform?.name || '本机运维 · 模型与组件' }}</p><h1>{{ pageTitle }}</h1></div>
        <div class="top-actions">
          <button class="refresh-button" :disabled="loadingOverview" @click="refreshStatus"><RefreshCw :class="{ spin: loadingOverview }" :size="16" /> 刷新状态</button>
          <div class="user-menu-wrap">
            <button class="user-button" aria-label="管理员菜单" :aria-expanded="menuOpen" @click="menuOpen = !menuOpen"><span>管</span><strong>管理员</strong><ChevronDown :size="15" /></button>
            <div v-if="menuOpen" class="user-menu"><button @click="reloadPage"><RefreshCw :size="15" />重新加载页面</button><button @click="logout"><LogOut :size="15" />退出登录</button></div>
          </div>
        </div>
      </header>
      <div v-if="updateAvailable" class="ui-update-notice" role="status"><span>ModelDeck 已更新，当前页面仍是旧版本。请保存未提交的内容后更新页面。</span><button class="secondary-button" @click="reloadPage">更新页面</button></div>
      <OperationStatus />
      <RouterView v-if="pageEnabled" />
      <div v-else class="page-body"><div class="notice">当前设备未启用此功能。<RouterLink to="/">返回运行总览</RouterLink></div></div>
    </main>
  </div>
</template>

<style scoped>
.ui-update-notice{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 24px;background:#fff4df;border-bottom:1px solid #e5ce9b;color:#72551a;font-size:13px}
</style>
