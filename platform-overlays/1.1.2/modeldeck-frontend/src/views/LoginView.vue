<script setup lang="ts">
import { ref } from 'vue'
import { Gauge, KeyRound, LoaderCircle, ShieldCheck } from '@lucide/vue'
import { api, describeError, setCsrf } from '../api'

const emit = defineEmits<{ authenticated: [] }>()
const password = ref('')
const busy = ref(false)
const error = ref('')

async function submit() {
  if (!password.value || busy.value) return
  busy.value = true
  error.value = ''
  try {
    const session = await api<{ csrf: string }>('/api/login', {
      method: 'POST',
      body: JSON.stringify({ password: password.value }),
    })
    setCsrf(session.csrf)
    password.value = ''
    emit('authenticated')
  } catch (reason) {
    error.value = describeError(reason) === 'invalid password' ? '管理员密码不正确' : describeError(reason)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="login-screen">
    <section class="login-story">
      <div class="login-brand"><span><Gauge :size="22" /></span><strong>ModelDeck</strong></div>
      <div class="story-copy">
        <p class="eyebrow light">MODELDECK · LOCAL CONTROL PLANE</p>
        <h1>让模型集群的状态，<br />一眼可见。</h1>
        <p>在设备本地查看运行状态、体验模型、管理接入密钥和核对调用记录。所有管理操作均留在内网。</p>
      </div>
      <div class="story-facts">
        <div><ShieldCheck :size="18" /><span><strong>受限运维</strong><small>无任意命令入口</small></span></div>
        <div><span class="fact-dot"></span><span><strong>离线可用</strong><small>不加载公网资源</small></span></div>
      </div>
    </section>

    <section class="login-form-wrap">
      <form class="login-card" @submit.prevent="submit">
        <div class="login-icon"><KeyRound :size="23" /></div>
        <p class="eyebrow">ADMIN ACCESS</p>
        <h2>登录管理台</h2>
        <p class="login-help">请输入交付时配置的管理员密码。</p>
        <label for="password">管理员密码</label>
        <input id="password" v-model="password" type="password" autocomplete="current-password" placeholder="输入密码" autofocus />
        <p v-if="error" class="form-error" role="alert">{{ error }}</p>
        <button class="primary-button login-submit" type="submit" :disabled="busy || !password">
          <LoaderCircle v-if="busy" class="spin" :size="16" />
          <span>{{ busy ? '正在验证' : '进入管理台' }}</span>
        </button>
        <small>会话闲置 8 小时后自动失效</small>
      </form>
    </section>
  </main>
</template>
