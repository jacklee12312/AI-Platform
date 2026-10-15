export interface KeyPolicy {
  application: string
  allowed_models: string[] | null
  expires_at: string | null
  rate_limit_per_minute: number
  max_concurrent_requests: number
  daily_request_limit: number
  monthly_token_warning: number
}

export interface PolicyModel { id: string; capability: string; modes: string[]; pools: string[] }
export interface ResourcePolicy {
  global_max_concurrent: number
  model_limits: Record<string, number>
  pool_limits: Record<string, number>
}
export interface ResourceSnapshot {
  policy: ResourcePolicy
  in_flight: { global: number; key: Record<string, number>; model: Record<string, number>; pool: Record<string, number> }
  models: PolicyModel[]
  default_rpm: number
  recent_rejections: { sequence: number; key_name: string; model: string; code: string; request_id: string; created_at: string }[]
}

export function defaultKeyPolicy(): KeyPolicy {
  return { application: '', allowed_models: null, expires_at: null, rate_limit_per_minute: 0,
    max_concurrent_requests: 0, daily_request_limit: 0, monthly_token_warning: 0 }
}

export function normalizePolicy(policy: KeyPolicy): KeyPolicy {
  const result = JSON.parse(JSON.stringify(policy)) as KeyPolicy
  for (const field of ['rate_limit_per_minute', 'max_concurrent_requests', 'daily_request_limit', 'monthly_token_warning'] as const) {
    const raw = result[field]
    const n = raw === ('' as unknown) ? 0 : Number(raw)
    if (!Number.isInteger(n) || n < 0 || n > (field === 'max_concurrent_requests' ? 1024 : 1_000_000_000)) throw new Error('限制值必须是范围内的非负整数')
    result[field] = n
  }
  if (result.expires_at && !Number.isFinite(Date.parse(result.expires_at))) throw new Error('请选择有效的到期时间')
  return result
}

export function rejectionLabel(code: string): string {
  return ({ model_not_allowed: '模型未授权', api_key_expired: 'Key 已到期', rate_limit_exceeded: '调用频率超限',
    daily_quota_exceeded: '当日额度用完', key_concurrency_exceeded: 'Key 并发超限', model_concurrency_exceeded: '模型并发超限',
    pool_concurrency_exceeded: '设备组并发超限', global_concurrency_exceeded: '网关总并发超限' } as Record<string, string>)[code] || code
}
