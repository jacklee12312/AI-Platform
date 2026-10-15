import type { ModelRecord } from './types'

export type ExperienceKind = 'chat' | 'vision' | 'cv' | 'ocr' | 'asr' | 'tts'
export const experiences: { id: ExperienceKind; title: string; detail: string }[] = [
  { id: 'chat', title: '对话生成', detail: '保留流式输出与调试参数' },
  { id: 'vision', title: '看图问答', detail: '图片与回答并排查看' },
  { id: 'cv', title: '视觉检测', detail: '检测框与结果列表联动' },
  { id: 'ocr', title: '文字识别', detail: '原图与识别文字对照' },
  { id: 'asr', title: '语音识别', detail: '原音频与转写对照' },
  { id: 'tts', title: '语音合成', detail: '输入文字，直接试听' },
]
export function matchesExperience(model: ModelRecord, kind: ExperienceKind): boolean {
  const vision = model.metadata?.vision === true || model.metadata?.type === 'vlm'
  if (kind === 'chat') return model.capability === 'chat' && (!vision || model.metadata?.text_chat === true)
  if (kind === 'vision') return model.capability === 'chat' && vision && model.metadata?.type !== 'document_ocr'
  if (kind === 'ocr') return model.capability === 'chat' && model.metadata?.type === 'document_ocr'
  if (kind === 'cv') return ['cv_classify', 'cv_detect', 'cv_face', 'cv_segment'].includes(model.capability)
  return model.capability === (kind === 'asr' ? 'audio_asr' : 'audio_tts')
}

export function isRecord(value: unknown): value is Record<string, any> { return !!value && typeof value === 'object' && !Array.isArray(value) }
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
export interface Detection { id: number; label: string; score: number; box: number[]; landmarks: number[][]; angle?: number }
export interface CVView { boxes: Detection[]; warnings: string[]; classifications: { class_id: number; score: number }[]; dimension?: number; hasGeometry: boolean }
export function cvView(raw: unknown, model: string, width: number, height: number): CVView {
  const view: CVView = { boxes: [], warnings: [], classifications: [], hasGeometry: false }
  if (!isRecord(raw) || raw.ok !== true || raw.model !== model || !isRecord(raw.result)) {
    view.warnings.push('响应结构或模型身份不匹配，未绘制检测框；请查看原始响应。'); return view
  }
  const result = raw.result
  if (Array.isArray(result.top5)) view.classifications = result.top5.filter((row: any) => isRecord(row) && Number.isInteger(row.class_id) && finite(row.score)).slice(0, 5)
  if (Number.isInteger(result.dimension) && result.dimension > 0) view.dimension = result.dimension
  if (result.mask_note) view.warnings.push('当前分割接口只返回检测框和掩码张量元数据，不绘制像素级掩码。')
  if (result.tracking_note) view.warnings.push('当前为单帧检测，不提供跨帧跟踪 ID。')
  const rows = result.detections ?? result.faces
  if (!Array.isArray(rows)) return view
  view.hasGeometry = true
  if (!Array.isArray(raw.original_size) || raw.original_size[0] !== width || raw.original_size[1] !== height) {
    view.warnings.push('接口坐标尺寸与本次图片不一致，已隐藏框以避免错位。'); view.hasGeometry = false; return view
  }
  let invalid = 0
  for (const [index, row] of rows.slice(0, 500).entries()) {
    if (!isRecord(row) || !Array.isArray(row.box) || row.box.length !== 4 || !row.box.every(finite)
      || !finite(row.score) || row.score < 0 || row.score > 1) { invalid++; continue }
    const [x1, y1, x2, y2] = row.box as [number, number, number, number]
    if (x2 <= x1 || y2 <= y1 || x2 <= 0 || y2 <= 0 || x1 >= width || y1 >= height) { invalid++; continue }
    const landmarks = Array.isArray(row.landmarks) ? row.landmarks.filter((p: any) => Array.isArray(p) && p.length === 2 && p.every(finite) && p[0] >= 0 && p[0] <= width && p[1] >= 0 && p[1] <= height).slice(0, 10) : []
    view.boxes.push({ id: index + 1, label: Array.isArray(result.faces) ? '人脸' : Number.isInteger(row.class_id) ? `类别 #${row.class_id}` : '目标',
      score: row.score, box: [Math.max(0, x1), Math.max(0, y1), Math.min(width, x2), Math.min(height, y2)], landmarks,
      angle: finite(row.angle_rad) ? row.angle_rad : undefined })
  }
  if (invalid) view.warnings.push(`${invalid} 条坐标或分数异常的结果已隐藏。`)
  if (rows.length > 500) view.warnings.push('仅展示前 500 条结果，请查看原始响应了解完整返回。')
  if (view.boxes.some(box => box.angle !== undefined)) view.warnings.push('旋转检测按接口的 XYXY 框展示；角度仅供参考，不伪造旋转轮廓。')
  return view
}

export function transcriptView(raw: unknown) {
  if (!isRecord(raw) || typeof raw.text !== 'string') throw new Error('接口未返回转写文本，请查看原始响应')
  const segments = (Array.isArray(raw.segments) ? raw.segments : []).filter((s: any) => isRecord(s) && typeof s.text === 'string'
    && finite(s.start) && finite(s.end) && s.start >= 0 && s.end > s.start && (!finite(raw.duration) || s.end <= raw.duration + .1)).slice(0, 500)
  return { text: raw.text, segments: segments as { start: number; end: number; text: string }[] }
}
export function seconds(value: number) { return `${Math.floor(value / 60)}:${(value % 60).toFixed(1).padStart(4, '0')}` }

export interface PreparedImage { blob: Blob; url: string; data: string; width: number; height: number; name: string }
export async function prepareImage(file: File): Promise<PreparedImage> {
  if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type) || file.size > 12 * 1024 * 1024) throw new Error('请选择不超过 12 MB 的 PNG、JPEG 或 WebP 图片')
  const url = URL.createObjectURL(file)
  try {
    const image = new Image(); image.src = url; await image.decode()
    const width = image.naturalWidth, height = image.naturalHeight
    if (!width || !height || width * height > 20_000_000 || Math.max(width, height) > 8192) throw new Error('图片最多 2000 万像素，单边不超过 8192 像素')
    // Canvas bakes browser-decoded EXIF orientation into the exact pixels sent to inference.
    const canvas = document.createElement('canvas'); canvas.width = width; canvas.height = height
    const context = canvas.getContext('2d'); if (!context) throw new Error('浏览器无法处理图片')
    context.fillStyle = '#fff'; context.fillRect(0, 0, width, height); context.drawImage(image, 0, 0)
    const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob(value => value ? resolve(value) : reject(new Error('图片处理失败')), 'image/png'))
    canvas.width = canvas.height = 1
    if (blob.size > 12 * 1024 * 1024) throw new Error('图片展开为 PNG 后超过 12 MB，请先缩小图片')
    const data = await new Promise<string>((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = () => reject(new Error('图片读取失败')); reader.readAsDataURL(blob) })
    return { blob, url: URL.createObjectURL(blob), data, width, height, name: file.name }
  } finally { URL.revokeObjectURL(url) }
}

export async function readResponse(response: Response, limit = 16 * 1024 * 1024): Promise<Uint8Array> {
  if (!response.body) throw new Error('浏览器没有收到响应')
  const reader = response.body.getReader(), chunks: Uint8Array[] = []; let size = 0
  try { while (true) { const item = await reader.read(); if (item.done) break; size += item.value.length; if (size > limit) throw new Error('响应过大，已停止接收；未重试'); chunks.push(item.value) } }
  finally { await reader.cancel().catch(() => undefined); reader.releaseLock() }
  const data = new Uint8Array(size); let offset = 0
  for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.length }
  return data
}

export async function streamChat(response: Response, onContent: (text: string) => void, onFrame: (frame: string) => void) {
  if (!response.body) throw new Error('浏览器没有收到响应流')
  const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '', size = 0, finished = false
  const frame = (value: string) => {
    const data = value.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
    if (!data) return
    onFrame(data)
    if (data.trim() === '[DONE]') { finished = true; return }
    const event = JSON.parse(data)
    if (event.error) throw new Error(typeof event.error.message === 'string' ? event.error.message : '流式接口返回错误')
    const text = event?.choices?.[0]?.delta?.content
    if (typeof text === 'string' && text) onContent(text)
  }
  try {
    while (!finished) {
      const part = await reader.read(); size += part.value?.length || 0
      if (size > 2 * 1024 * 1024) throw new Error('流式响应超过 2 MB，已停止接收')
      buffer += decoder.decode(part.value || new Uint8Array(), { stream: !part.done })
      // Normalize CRLF only after complete bytes have accumulated, including split CR/LF.
      let match: RegExpExecArray | null
      while ((match = /\r?\n\r?\n/.exec(buffer))) { frame(buffer.slice(0, match.index).replace(/\r\n/g, '\n')); buffer = buffer.slice(match.index + match[0].length); if (finished) break }
      if (part.done) { if (buffer.trim() && !finished) frame(buffer.replace(/\r\n/g, '\n')); break }
    }
    if (!finished) throw new Error('响应流在 [DONE] 之前中断，内容可能不完整；未重试')
  } finally { await reader.cancel().catch(() => undefined); reader.releaseLock() }
}
