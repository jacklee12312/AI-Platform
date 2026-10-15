export type EvaluationSuite = 'smoke' | 'quality' | 'repeat'
export type RunStatus = 'queued' | 'running' | 'cancelling' | 'completed' | 'failed' | 'cancelled' | 'interrupted'
export type CaseStatus = 'pending' | 'running' | 'passed' | 'failed' | 'skipped' | 'interrupted'
export type Verdict = 'pending' | 'passed' | 'failed' | 'incomplete'

export interface EvaluationModel {
  id: string
  device: string
  capability: string
  status: string
  available: boolean
  reason: string | null
  suites: string[]
}

export interface EvaluationCatalog {
  version: string
  models: EvaluationModel[]
  suites: { id: EvaluationSuite; name: string; description: string; scope: string }[]
  limits: { max_models: number; max_repeats: number; max_cases: number }
  active_run_id: string | null
}

export interface EvaluationCase {
  id: string
  model: string
  name: string
  scope: 'execution' | 'quality'
  status: CaseStatus
  reason?: string | null
  input_sha256: string
  expected: unknown
  actual?: unknown
  latency_ms?: number | null
  parameters: Record<string, unknown>
  sample?: unknown
  metrics?: unknown
}

export interface EvaluationRun {
  id: string
  status: RunStatus
  verdict: Verdict
  created_at: string | number
  started_at?: string | number | null
  finished_at?: string | number | null
  suite: EvaluationSuite
  models: string[]
  repeats: number
  version: string
  summary: {
    total: number; passed: number; failed: number; skipped: number; pending: number
    latency_ms: { count: number; mean: number | null; p50: number | null; p95: number | null; min: number | null; max: number | null }
  }
  cases: EvaluationCase[]
  snapshot: Record<string, unknown>
  error?: string
}

export interface EvaluationPlan { models: string[]; suite: EvaluationSuite; repeats: number }
export interface EvaluationListing { runs: EvaluationRun[]; active_run_id: string | null }

export const runStatusLabels: Record<RunStatus, string> = {
  queued: '排队中', running: '评测中', cancelling: '等待当前请求结束', completed: '已结束',
  failed: '执行失败', cancelled: '已取消', interrupted: '执行中断',
}
export const caseStatusLabels: Record<CaseStatus, string> = {
  pending: '待执行', running: '执行中', passed: '通过', failed: '失败', skipped: '已跳过', interrupted: '已中断',
}
export const verdictLabels: Record<Verdict, string> = {
  pending: '结论待定', passed: '全部通过', failed: '未通过', incomplete: '评测不完整',
}
export const suiteLabels: Record<EvaluationSuite, string> = { smoke: '快速自检', quality: '固定样例评测', repeat: '重复时延采样' }

export function isActiveRun(run: Pick<EvaluationRun, 'status'>): boolean {
  return ['queued', 'running', 'cancelling'].includes(run.status)
}

// A completed job is not proof of a passed evaluation. Check coverage as well.
export function evaluationVerdict(run: EvaluationRun): Verdict {
  if (isActiveRun(run)) return 'pending'
  if (run.status === 'failed' || run.verdict === 'failed' || run.summary.failed > 0 || run.cases.some(item => item.status === 'failed')) return 'failed'
  if (run.status !== 'completed' || run.verdict !== 'passed' || run.summary.total <= 0
    || run.summary.skipped !== 0 || run.summary.pending !== 0 || run.summary.passed !== run.summary.total
    || run.cases.length !== run.summary.total || run.cases.some(item => item.status !== 'passed')) return 'incomplete'
  return 'passed'
}

export function modelUnavailableReason(model: EvaluationModel, suite: EvaluationSuite): string {
  if (!model.available) return model.reason || '当前不可用，未提供具体原因'
  if (!model.suites.includes(suite)) return model.reason || '不支持当前评测套件'
  return ''
}

export function evaluationSelectionError(catalog: EvaluationCatalog | null, plan: EvaluationPlan): string {
  if (!catalog) return '请先成功刷新模型目录。'
  if (!catalog.suites.some(item => item.id === plan.suite)) return '当前目录不支持该评测套件。'
  if (!plan.models.length) return '请至少选择一个可用模型。'
  const limit = Math.min(23, catalog.limits.max_models)
  if (plan.models.length > limit) return `一次最多选择 ${limit} 个模型。`
  if (new Set(plan.models).size !== plan.models.length) return '模型选择包含重复项。'
  for (const id of plan.models) {
    const model = catalog.models.find(item => item.id === id)
    if (!model) return `${id} 已不在当前目录中，请取消选择。`
    const reason = modelUnavailableReason(model, plan.suite)
    if (reason) return `${id}：${reason}。请调整选择。`
  }
  if (!Number.isInteger(plan.repeats) || plan.repeats < 1 || plan.repeats > Math.min(3, catalog.limits.max_repeats)) return '重复次数必须在当前允许的 1–3 次范围内。'
  if (plan.suite !== 'repeat' && plan.repeats !== 1) return '仅重复评测支持多次执行。'
  return ''
}

export function newEvaluationId(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, '0')).join('')
}

export function evaluationTime(value: string | number | null | undefined): string {
  if (value == null || value === '') return '未记录'
  const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value)
  return Number.isNaN(date.getTime()) ? '未记录' : date.toLocaleString('zh-CN', { hour12: false })
}

export function latencyText(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) ? '—' : `${value.toLocaleString('zh-CN', { maximumFractionDigits: 1 })} ms`
}

export function evidenceText(value: unknown): string {
  if (value === undefined) return '未记录'
  if (typeof value === 'string') return value || '（空字符串）'
  return JSON.stringify(value, null, 2)
}
