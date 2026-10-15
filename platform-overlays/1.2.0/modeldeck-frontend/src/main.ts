import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import './style.css'
import './ops.css'
import DeviceView from './views/DeviceView.vue'
import ComponentsView from './views/ComponentsView.vue'
import TasksView from './views/TasksView.vue'
import App from './App.vue'
import DashboardView from './views/DashboardView.vue'
import ModelsView from './views/ModelsView.vue'
import ModelInstallView from './views/ModelInstallView.vue'
import TestConsoleView from './views/TestConsoleView.vue'
import ConfigView from './views/ConfigView.vue'
import LogsView from './views/LogsView.vue'
import UsageView from './views/UsageView.vue'
import BatchView from './views/BatchView.vue'
import CapabilitiesView from './views/CapabilitiesView.vue'
import DiagnosticsView from './views/DiagnosticsView.vue'
import EvaluationsView from './views/EvaluationsView.vue'
import ApiReferenceView from './views/ApiReferenceView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: DeviceView, meta: { title: '设备总览' } },
    { path: '/runtime', component: DashboardView, meta: { title: '模型运行与硬件' } },
    { path: '/components', component: ComponentsView, meta: { title: '组件中心' } },
    { path: '/tasks', component: TasksView, meta: { title: '任务中心' } },
    { path: '/models', component: ModelsView, meta: { title: '模型中心 · 已安装' } },
    { path: '/model-install', component: ModelInstallView, meta: { title: '模型中心 · 补装模型' } },
    { path: '/capabilities', component: CapabilitiesView, meta: { title: '模型能力与示例' } },
    { path: '/api-reference', component: ApiReferenceView, meta: { title: '完整 API 参考' } },
    { path: '/diagnostics', component: DiagnosticsView, meta: { title: '运行基线与诊断' } },
    { path: '/evaluations', component: EvaluationsView, meta: { title: '自检与评测' } },
    { path: '/usage', component: UsageView, meta: { title: 'API Key 与用量' } },
    { path: '/batches', component: BatchView, meta: { title: '批量任务' } },
    { path: '/test', component: TestConsoleView, meta: { title: '模型体验' } },
    { path: '/config', component: ConfigView, meta: { title: '配置管理' } },
    { path: '/logs', component: LogsView, meta: { title: '日志审计' } },
    { path: '/:pathMatch(.*)*', redirect: '/' },
  ],
})

createApp(App).use(router).mount('#app')
