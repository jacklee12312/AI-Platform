<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { api, describeError } from '../api'
import { overview as runtimeOverview } from '../store'
type Disk = { path: string; total: number; used: number; free: number; percent: number }
type Metric = { at: number; cpu_percent: number; memory_percent: number; temperature_c: number | null; disks: Disk[]; hostname: string; uptime_seconds: number }
type Component = { id: string; name: string; status: string; running: boolean }
type Alert = { id: string; message: string; started_at: number; resolved_at: number | null }
type Snapshot = { version: string; device: Metric; history: Metric[]; components: Component[]; alerts: Alert[]; probe: { running: boolean; registered: boolean; mqtt_connected: boolean | null; device_id: string; platform: string; checked_at: number; last_report_at: number | null } }
const value = ref<Snapshot | null>(null), error = ref(''), loading = ref(false)
let timer: ReturnType<typeof setInterval> | undefined
const activeAlerts = computed(() => value.value?.alerts.filter(a => !a.resolved_at) || [])
const accelerators = computed(() => runtimeOverview.value?.host.accelerators || [])
const healthy = computed(() => value.value?.components.filter(c => c.status === 'healthy').length || 0)
const bytes = (n: number) => `${(n / 1024 ** 3).toFixed(1)} GiB`
const time = (n: number) => new Date(n * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' })
function line(key: 'cpu_percent' | 'memory_percent') { const rows = value.value?.history || []; return rows.map((r, i) => `${i * 600 / Math.max(rows.length - 1, 1)},${100 - r[key]}`).join(' ') }
async function refresh() { if (loading.value) return; loading.value = true; try { value.value = await api<Snapshot>('/api/ops/overview'); error.value = '' } catch (e) { error.value = describeError(e) } finally { loading.value = false } }
onMounted(() => { refresh(); timer = setInterval(refresh, 30000) }); onUnmounted(() => clearInterval(timer))
</script>
<template>
  <section class="page-body ops-page">
    <div v-if="error" class="notice error-notice" role="alert">{{ error }}<span v-if="value"> · 以下为上次成功读取的数据</span></div>
    <section class="panel device-heading"><div><small>当前设备 · ModelDeck v1.2.0</small><h2>{{ value?.device.hostname || '正在读取设备' }}</h2><p>{{ value?.probe.device_id || '设备身份待探针确认' }}</p></div><div><span :class="['ops-badge', value?.probe.mqtt_connected === true ? 'good' : 'warn']">{{ value?.probe.mqtt_connected === true ? 'AI STORE 已连接' : value?.probe.mqtt_connected === false ? 'AI STORE 未连接' : 'AI STORE 连接状态未知' }}</span><button class="secondary-button" :disabled="loading" @click="refresh">{{ loading ? '读取中…' : '刷新状态' }}</button></div></section>
    <div class="ops-grid metrics-grid" v-if="value"><article class="panel metric"><span>CPU 使用率</span><strong>{{ value.device.cpu_percent }}<small>%</small></strong></article><article class="panel metric"><span>内存使用率</span><strong>{{ value.device.memory_percent }}<small>%</small></strong></article><article class="panel metric"><span>设备温度</span><strong>{{ value.device.temperature_c ?? '—' }}<small>°C</small></strong></article><article class="panel metric"><span>加速卡</span><strong>{{ accelerators.length || '—' }}</strong><RouterLink to="/runtime">查看运行模式与硬件状态 →</RouterLink></article></div>
    <div class="ops-grid two-columns">
      <section class="panel ops-section"><h2>探针与平台连接</h2><dl v-if="value"><dt>探针服务</dt><dd>{{ value.probe.running ? '运行中' : '未运行' }}</dd><dt>设备注册</dt><dd>{{ value.probe.registered ? '本机已有注册记录' : '未完成' }}</dd><dt>MQTT 连接</dt><dd>{{ value.probe.mqtt_connected === null ? '无法确认' : value.probe.mqtt_connected ? '已连接' : '未连接' }}</dd><dt>接入平台</dt><dd>{{ value.probe.platform || '—' }}</dd><dt>状态采集时间</dt><dd>{{ time(value.probe.checked_at) }}</dd><dt>运行时长</dt><dd>{{ Math.floor(value.device.uptime_seconds / 3600) }} 小时</dd></dl><p>设备运行数据保存在本机；平台断连时仍可进行本地运维。</p></section>
      <section class="panel ops-section"><h2>组件实例 <small>{{ healthy }} 个健康</small></h2><div v-for="c in value?.components" :key="c.id" class="ops-row"><strong>{{ c.name }}</strong><span :class="['ops-badge', c.status === 'healthy' ? 'good' : c.status === 'unhealthy' ? 'warn' : '']">{{ ({ healthy: '健康', stopped: '已停止', not_installed: '未安装', external: '外部部署', unhealthy: '检查异常', unknown: '状态未知' } as Record<string,string>)[c.status] }}</span></div><RouterLink to="/components">管理组件及补装 →</RouterLink></section>
    </div>
    <section class="panel ops-section"><h2>资源趋势 <small>每 30 秒采集 · 展示最近 24 小时，保留 7 天</small></h2><p v-if="!value?.history.length">等待本机首次采集</p><template v-else><div class="chart-legend"><span>● CPU</span><span>● 内存</span><small>纵轴 0–100%</small></div><svg class="ops-chart" viewBox="0 0 600 100" preserveAspectRatio="none" role="img" aria-label="CPU 和内存使用率趋势"><path d="M0 25H600M0 50H600M0 75H600" stroke="#e5ebe9" fill="none"/><polyline :points="line('cpu_percent')" fill="none" stroke="#267c69" stroke-width="1.5"/><polyline :points="line('memory_percent')" fill="none" stroke="#8173bf" stroke-width="1.5"/></svg><small>{{ time(value.history[0].at) }} — {{ time(value.history[value.history.length-1].at) }}</small></template></section>
    <div class="ops-grid two-columns"><section class="panel ops-section"><h2>磁盘空间</h2><div v-for="d in value?.device.disks" :key="d.path" class="disk-row"><div><strong>{{ d.path }}</strong><span>{{ d.percent }}% · 剩余 {{ bytes(d.free) }}</span></div><progress :value="d.percent" max="100" /></div></section><section class="panel ops-section"><h2>告警 <small>{{ activeAlerts.length }} 个待处理</small></h2><p v-if="!value?.alerts.length">暂无告警记录</p><div v-for="a in value?.alerts" :key="a.id" class="ops-row"><span>{{ a.message }}<small>{{ time(a.started_at) }}</small></span><span :class="['ops-badge', a.resolved_at ? 'good' : 'warn']">{{ a.resolved_at ? '已恢复' : '待处理' }}</span></div></section></div>
  </section>
</template>
