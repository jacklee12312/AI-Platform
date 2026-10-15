<script setup lang="ts">
import { onMounted } from 'vue'
import { operationState as task, operations } from './operations'
function acknowledge() {
  if (window.confirm('请先核查日志审计和实际服务。这里只解除页面操作锁，不会取消或重新执行后台任务。确认已核查？')) operations.acknowledge()
}
onMounted(() => { operations.resume().catch(() => undefined) })
</script>
<template>
  <section v-if="task.label" class="operation-status panel" role="status" aria-live="polite">
    <strong>{{ task.label }} · {{ task.busy ? '执行中或待核查' : task.job?.status === 'succeeded' ? '已完成' : '未完成' }}</strong>
    <p>{{ task.job?.progress?.stage || (task.busy ? '等待后台结果，切换页面不会重新提交任务' : '') }}</p>
    <p v-if="task.job?.progress?.current_model">当前模型：<code>{{ task.job.progress.current_model }}</code><span v-if="task.job.progress.elapsed_seconds != null"> · 已用时 {{ Math.floor(task.job.progress.elapsed_seconds) }} 秒</span></p>
    <p v-if="task.job">任务 ID：<code>{{ task.job.id }}</code></p>
    <p v-if="task.job?.progress?.file">{{ task.job.progress.file }}</p>
    <p v-if="task.job?.progress?.total_bytes"><progress :value="task.job.progress.bytes || 0" :max="task.job.progress.total_bytes"></progress> {{ Math.floor((task.job.progress.bytes || 0) / task.job.progress.total_bytes * 100) }}%</p>
    <p v-if="task.error" role="alert">{{ task.error }}</p>
    <div v-if="task.busy && !task.querying">
      <button v-if="task.job" class="secondary-button" @click="operations.track(task.job.id).catch(() => undefined)">继续查询任务</button>
      <button class="secondary-button" @click="acknowledge">已人工核查，解除页面锁</button>
      <RouterLink to="/logs">查看日志审计</RouterLink>
    </div>
  </section>
</template>
<style scoped>
.operation-status { margin: 16px 24px 0; padding: 14px 18px; border-color: #c4dcd2; }
p { margin-top: 6px; font-size: 12px; overflow-wrap: anywhere; }
[role=alert] { color: #a14343; } button,a { margin-right: 12px; margin-top: 8px; }
</style>
