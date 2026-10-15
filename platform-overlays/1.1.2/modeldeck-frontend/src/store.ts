import { computed, ref } from 'vue'
import { api } from './api'
import type { ModelsResponse, Overview } from './types'

export const overview = ref<Overview | null>(null)
export const models = ref<ModelsResponse>({ object: 'list', data: [] })
export const loadingOverview = ref(false)
export const overviewError = ref('')
export const lastRefresh = computed(() => overview.value?.refreshed_at || '')

export async function refreshOverview() {
  loadingOverview.value = true
  overviewError.value = ''
  try {
    const [nextOverview, nextModels] = await Promise.all([
      api<Overview>('/api/overview'),
      api<ModelsResponse>('/api/models'),
    ])
    overview.value = nextOverview
    models.value = nextModels
  } catch (error) {
    overviewError.value = error instanceof Error ? error.message : '状态刷新失败'
    throw error
  } finally {
    loadingOverview.value = false
  }
}
