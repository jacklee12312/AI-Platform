export function tokenText(value: number | null | undefined) {
  return value == null ? '未知' : new Intl.NumberFormat('zh-CN').format(value)
}
export function usageQuality(value: string | undefined) {
  return ({ reported: '上游已报告', derived: '含推导值', partial: '计数不完整', missing: '未报告', invalid: '格式无效', inconsistent: '计数不一致', legacy: '历史口径未核验' } as Record<string,string>)[value || 'legacy'] || value
}
export function requestStatus(value: string) {
  if (value === 'in_flight') return '在途'
  if (value === 'ended_unrecorded') return '已离开在途列表'
  return ({ succeeded: '成功', rejected: '已拒绝', failed: '失败', upstream_error: '上游错误', stream_error: '流式错误', interrupted: '流未完整结束', cancelled: '已取消', invalid_response: '无效响应' } as Record<string,string>)[value] || value
}
export function dateBoundaries(start: string, end: string) {
  if (start && end && start > end) throw new Error('开始日期不能晚于结束日期')
  function convert(value: string, last: boolean) {
    if (!value) return ''
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new Error('日期格式无效')
    const date = new Date(`${value}T${last ? '23:59:59.999' : '00:00:00'}`)
    if (!Number.isFinite(date.getTime()) || `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}` !== value) throw new Error('日期格式无效')
    return date.toISOString()
  }
  return { start: convert(start, false), end: convert(end, true) }
}
