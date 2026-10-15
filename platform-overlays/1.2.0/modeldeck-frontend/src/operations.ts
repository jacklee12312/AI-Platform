import { reactive } from 'vue'
import { api } from './api'
import { createJobTracker, type TrackingState } from './jobTracker'

export const operationState = reactive<TrackingState>({ busy: false, querying: false, label: '', job: null, error: '' })
export const operations = createJobTracker(operationState, api, {
  getItem: key => window.sessionStorage.getItem(key),
  setItem: (key, value) => window.sessionStorage.setItem(key, value),
  removeItem: key => window.sessionStorage.removeItem(key),
})
