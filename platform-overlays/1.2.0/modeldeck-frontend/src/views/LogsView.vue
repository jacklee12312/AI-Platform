<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ClipboardList, LoaderCircle, RefreshCw, ScrollText, Search, Terminal, TriangleAlert } from '@lucide/vue'
import { api, describeError } from '../api'
import { overview } from '../store'

interface AuditRow { id: string; created_at: string; updated_at: string; actor: string; action: string; target: string; status: string; request_id: string; job_id: string; detail: string }

const tab = ref<'logs' | 'audit'>('logs')
const service = ref('gateway')
const tail = ref(200)
const lines = ref<string[]>([])
const audits = ref<AuditRow[]>([])
const query = ref('')
const auditQuery = ref('')
const loading = ref(false)
const error = ref('')

const defaultServices = [
  ['gateway', '模型网关'], ['copaw', 'CoPaw'], ['qwen-vl', 'Qwen3-VL'],
  ['qwen38', 'Qwen3.8'], ['qwen36', 'Qwen3.6 双卡'], ['spark-x1', 'Spark X1'], ['bge-m3', 'BGE M3'], ['reranker', 'Reranker'], ['cosyvoice', 'CosyVoice3'], ['cv', 'CV 服务'], ['sensevoice', 'SenseVoice'],
  ['glm-ocr', 'GLM-OCR'], ['matcha', 'Matcha 中文语音'], ['kokoro', 'Kokoro 语音'],
]
const services = computed(() => {
  const rows = overview.value?.host.containers || []
  return rows.length ? rows.map(item => [item.service, item.service]) : defaultServices
})

const filteredLines = computed(() => query.value ? lines.value.filter(line => line.toLowerCase().includes(query.value.toLowerCase())) : lines.value)

function actionName(value: string) {
  if (value === 'device-recovery') return '设备故障恢复'
  const batch = { 'batch-create': '创建批量任务', 'batch-submit': '提交批量任务', 'batch-cancel': '取消批量任务', 'batch-delete': '删除批量任务', 'batch-download': '下载批量结果', 'batch-file': '批量文件处理', 'batch-operation': '批量任务操作' } as Record<string, string>
  if (batch[value]) return batch[value]
  return ({ login: '管理员登录', logout: '退出登录', 'mode-switch': '切换模式', restart: '重启服务', 'config-apply': '应用配置', 'api-test': '接口测试', 'api-key-create': '新建 API Key', 'api-key-update': '修改 API Key', 'api-key-revoke': '撤销 API Key', 'request-cancel': '中断在途请求' } as Record<string,string>)[value] || value
}

function statusName(value: string) {
  return ({ accepted: '已提交', queued: '排队中', running: '执行中', started: '已开始', succeeded: '成功', failed: '失败', unknown: '结果未知', cancelling: '中断处理中', cancelled: '已取消', interrupted: '执行中断', already_finished: '请求已结束' } as Record<string,string>)[value] || value
}

function auditDetail(row: AuditRow) {
  try { return JSON.stringify(JSON.parse(row.detail), null, 2) } catch { return row.detail }
}

async function loadLogs() {
  loading.value = true
  error.value = ''
  try {
    const result = await api<{ lines: string[] }>(`/api/logs?service=${encodeURIComponent(service.value)}&tail=${tail.value}`)
    lines.value = result.lines
  } catch (reason) { error.value = describeError(reason) } finally { loading.value = false }
}

async function loadAudit() {
  loading.value = true
  error.value = ''
  try {
    const result = await api<{ data: AuditRow[] }>(`/api/audit?limit=200&q=${encodeURIComponent(auditQuery.value.trim())}`)
    audits.value = result.data
  } catch (reason) { error.value = describeError(reason) } finally { loading.value = false }
}

function changeTab(value: 'logs' | 'audit') {
  tab.value = value
  error.value = ''
  if (value === 'logs') loadLogs(); else loadAudit()
}

onMounted(loadLogs)
</script>

<template>
  <section class="page-body">
    <div v-if="error" class="notice error-notice"><TriangleAlert :size="16" />{{ error }}</div>
    <section class="panel logs-panel">
      <div class="tabs-bar"><button :class="{ active: tab === 'logs' }" @click="changeTab('logs')"><Terminal :size="16" />服务日志</button><button :class="{ active: tab === 'audit' }" @click="changeTab('audit')"><ClipboardList :size="16" />操作审计</button></div>

      <template v-if="tab === 'logs'">
        <div class="log-toolbar">
          <label>服务<select v-model="service" @change="loadLogs"><option v-for="item in services" :key="item[0]" :value="item[0]">{{ item[1] }}</option></select></label>
          <label>行数<select v-model="tail" @change="loadLogs"><option :value="100">100</option><option :value="200">200</option><option :value="500">500</option><option :value="1000">1000</option></select></label>
          <div class="search-box log-search"><Search :size="15" /><input v-model="query" placeholder="筛选当前日志" /></div>
          <button class="secondary-button" :disabled="loading" @click="loadLogs"><LoaderCircle v-if="loading" class="spin" :size="15" /><RefreshCw v-else :size="15" />刷新</button>
        </div>
        <div class="log-terminal" aria-live="polite"><div class="terminal-label"><ScrollText :size="14" /><span>{{ service }} · {{ filteredLines.length }} lines</span></div><pre>{{ filteredLines.join('\n') || (loading ? '正在读取日志…' : '当前没有日志') }}</pre></div>
      </template>

      <template v-else>
        <div class="audit-head"><div><h2>管理操作记录</h2><p>登录、模式切换、重启、配置与 Key 操作；后台持续记录任务结果</p></div><button class="secondary-button" :disabled="loading" @click="loadAudit"><LoaderCircle v-if="loading" class="spin" :size="15" /><RefreshCw v-else :size="15" />刷新</button></div>
        <form class="audit-search" @submit.prevent="loadAudit"><label for="audit-query">查询操作记录</label><input id="audit-query" v-model="auditQuery" maxlength="200" placeholder="输入 Request ID、任务 ID、目标或关键词" /><button class="secondary-button" :disabled="loading" type="submit"><Search :size="15" />查询</button><span>显示最近 200 条匹配记录；展开详情查看结果及错误原因。</span></form>
        <div class="table-wrap">
          <table class="audit-table"><thead><tr><th>时间</th><th>操作</th><th>目标</th><th>操作者</th><th>结果</th><th>关联 ID / 详情</th></tr></thead>
            <tbody><tr v-for="row in audits" :key="row.id"><td class="mono">{{ row.created_at.replace('T',' ') }}</td><td>{{ actionName(row.action) }}</td><td><code>{{ row.target }}</code></td><td>{{ row.actor }}</td><td><span :class="['audit-status', row.status]">{{ statusName(row.status) }}</span></td><td><details class="audit-detail"><summary :aria-label="`查看 ${row.request_id} 的审计详情`"><code>{{ row.request_id }}</code><span v-if="row.job_id">任务：<code>{{ row.job_id }}</code></span></summary><p v-if="row.updated_at">最近更新：{{ row.updated_at.replace('T',' ') }}</p><p v-if="row.status === 'unknown'">最终结果无法确认，请核查服务状态；不要直接重复执行操作。</p><pre>{{ auditDetail(row) }}</pre></details></td></tr><tr v-if="!audits.length"><td colspan="6" class="empty-row">{{ loading ? '正在读取审计记录…' : '暂无匹配的操作记录' }}</td></tr></tbody>
          </table>
        </div>
      </template>
    </section>
  </section>
</template>

<style scoped>
.audit-head { gap: 12px; }
.audit-head button { flex-shrink: 0; white-space: nowrap; }
.audit-search { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 0 16px 16px; }
.audit-search label, .audit-search span { font-size: 12px; color: #65747a; }
.audit-search input { flex: 1; min-width: 240px; padding: 8px 10px; border: 1px solid #d5dfe1; border-radius: 6px; }
.audit-search span { flex-basis: 100%; }
.audit-detail { max-width: 440px; min-width: 180px; }
.audit-detail summary { cursor: pointer; overflow-wrap: anywhere; white-space: normal; }
.audit-detail summary span { display: block; margin-top: 4px; }
.audit-detail p { font-size: 12px; white-space: normal; margin-top: 8px; }
.audit-detail pre { max-height: 240px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 12px; }
.audit-status.accepted, .audit-status.queued, .audit-status.running, .audit-status.started { background: #eaf2fa; color: #315e86; }
.audit-status.unknown { background: #fff2d8; color: #855a0b; }
</style>
