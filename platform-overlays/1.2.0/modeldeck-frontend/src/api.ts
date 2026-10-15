let csrfToken = ''

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, detail: unknown) {
    super(typeof detail === 'string' ? detail : `请求失败（HTTP ${status}）`)
    this.status = status
    this.detail = detail
  }
}

export function setCsrf(value: string) {
  csrfToken = value
}

export function getCsrf() {
  return csrfToken
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const method = (init.method || 'GET').toUpperCase()
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && csrfToken) headers.set('X-CSRF-Token', csrfToken)
  const response = await fetch(path, { ...init, headers, credentials: 'include' })
  const contentType = response.headers.get('content-type') || ''
  const payload = contentType.includes('json') ? await response.json() : await response.text()
  if (!response.ok) {
    const detail = payload && typeof payload === 'object' && 'detail' in payload ? payload.detail : payload
    throw new ApiError(response.status, detail)
  }
  return payload as T
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.detail && typeof error.detail === 'object' && 'detail' in error.detail) {
      return describeError(new ApiError(error.status, (error.detail as { detail: unknown }).detail))
    }
    if (typeof error.detail === 'string') return error.detail
    if (Array.isArray(error.detail)) return error.detail.slice(0, 4).map(item => `${Array.isArray(item.loc) ? item.loc.filter((part: string) => part !== 'body').join('.') : '参数'}：${item.msg || '校验失败'}`).join('；')
    if (error.detail && typeof error.detail === 'object') {
      const value = error.detail as Record<string, unknown>
      if (typeof value.message === 'string') return value.message
      if (value.error && typeof value.error === 'object' && typeof (value.error as Record<string, unknown>).message === 'string') {
        return (value.error as Record<string, string>).message
      }
    }
  }
  return error instanceof Error ? error.message : '未知错误'
}
