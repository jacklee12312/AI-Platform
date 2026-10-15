<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { api, describeError } from '../api'
import { operations, operationState } from '../operations'
import type { Job } from '../types'
const rows=ref<Job[]>([]), error=ref(''); let timer: ReturnType<typeof setInterval> | undefined
const names: Record<string,string>={ queued:'等待执行', running:'执行中', succeeded:'成功', failed:'失败', unknown:'中断 · 结果待核查' }
async function refresh() { try { rows.value=(await api<{data:Job[]}>('/api/ops/tasks')).data; error.value='' } catch(e) { error.value=describeError(e) } }
async function retry(row: Job) { if (operationState.busy) return; try { await operations.submit('/api/model-install/actions/download',{method:'POST',body:JSON.stringify({ids:row.target.split(','),activate:false})},'续传模型下载'); await refresh() } catch(e) { error.value=describeError(e) } }
onMounted(()=>{refresh();timer=setInterval(refresh,5000)});onUnmounted(()=>clearInterval(timer))
</script>
<template><section class="page-body ops-page"><div v-if="error" class="notice error-notice" role="alert">{{error}}</div><section class="panel ops-section"><div class="panel-head"><div><h2>安装与运维任务</h2><p>任务记录保存在设备。中断后先核查状态；下载可续传，安装或服务操作需从对应模块重新预检。</p></div><button class="secondary-button" @click="refresh">刷新</button></div><p v-if="!rows.length">暂无任务</p><article v-for="row in rows" :key="row.id" class="task-row"><div><strong>{{row.kind}} · {{row.target}}</strong><small>{{row.created_at}} · {{row.id}}</small><p v-if="row.progress">{{row.progress.stage}} <span v-if="row.progress.total_bytes">{{Math.floor((row.progress.bytes || 0)/row.progress.total_bytes*100)}}%</span></p><p v-if="row.error" class="warning">{{row.error}}</p></div><span :class="['ops-badge',row.status==='succeeded'?'good':row.status==='failed'||row.status==='unknown'?'warn':'']">{{names[row.status] || row.status}}</span><button v-if="row.kind==='model-download' && ['failed','unknown'].includes(row.status)" class="secondary-button" :disabled="operationState.busy" @click="retry(row)">重试续传</button><RouterLink v-if="['failed','unknown'].includes(row.status)" :to="row.kind.startsWith('component-')?'/components':'/model-install'">查看并恢复</RouterLink></article></section></section></template>
