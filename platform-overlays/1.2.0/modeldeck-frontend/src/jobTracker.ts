import type { Job } from './types'

export interface TrackingState { busy: boolean; querying: boolean; label: string; job: Job | null; error: string }
type Transport = <T>(path: string, init?: RequestInit) => Promise<T>
type StoragePort = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
const key = 'modeldeck-management-job'

export function createJobTracker(state: TrackingState, request: Transport, storage: StoragePort,
  delay: () => Promise<void> = () => new Promise(resolve => setTimeout(resolve, 1500))) {
  let polling: Promise<Job> | null = null
  function remember(id: string | null) {
    try { storage.setItem(key, JSON.stringify({ id, label: state.label })) } catch { /* keep live task in memory */ }
  }
  function forget() { try { storage.removeItem(key) } catch { /* storage can be unavailable */ } }
  async function query(id: string): Promise<Job> {
    state.querying = true
    state.busy = true
    state.error = ''
    try {
      for (let attempt = 0; attempt < 1200; attempt++) {
        const job = await request<Job>(`/api/jobs/${encodeURIComponent(id)}`)
        state.job = job
        if (job.status === 'succeeded' || job.status === 'failed') {
          state.busy = false
          forget()
          if (job.status === 'failed') throw new Error(job.error || '操作失败，请检查日志')
          return job
        }
        if (job.status === 'unknown') throw new Error(job.error || '任务结果未知，请人工核查；不要重复提交')
        await delay()
      }
      throw new Error('页面等待已结束，后台可能仍在执行；请继续查询，不要重复提交')
    } catch (error) {
      state.error = error instanceof Error ? error.message : '任务查询失败'
      throw error
    } finally { state.querying = false }
  }
  function track(id: string) {
    if (!polling) polling = query(id).finally(() => { polling = null })
    return polling
  }
  async function submit(path: string, init: RequestInit, label: string) {
    if (state.busy) throw new Error('另一个管理任务尚未确认结束，请先查询或核查')
    state.busy = true
    state.job = null
    state.label = label
    state.error = ''
    remember(null)
    let job: Job
    try {
      job = await request<Job>(path, init)
      if (!job.id) throw new Error('提交响应缺少任务 ID，结果未知')
      state.job = job
      remember(job.id)
    } catch (error) {
      const status = (error as { status?: number })?.status
      // A clear 4xx denial did not accept the job. A transport/5xx failure is
      // ambiguous: leave the UI locked instead of automatically resubmitting.
      if (status && status >= 400 && status < 500) { state.busy = false; forget() }
      state.error = error instanceof Error ? error.message : '操作提交失败'
      throw error
    }
    return track(job.id)
  }
  async function resume() {
    if (polling) return polling
    let saved: { id: string | null; label: string } | null = null
    try { saved = JSON.parse(storage.getItem(key) || 'null') } catch { forget() }
    if (!saved) return
    state.label = saved.label
    state.busy = true
    if (saved.id && /^[a-zA-Z0-9_-]{1,128}$/.test(saved.id)) return track(saved.id)
    state.error = '上次提交的结果未知，请先检查日志审计和实际服务；不要直接重复提交'
  }
  function acknowledge() {
    if (state.querying) return
    state.busy = false
    state.error = ''
    forget()
  }
  return { submit, track, resume, acknowledge }
}
