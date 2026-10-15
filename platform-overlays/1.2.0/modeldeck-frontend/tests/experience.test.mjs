import test from 'node:test'
import assert from 'node:assert/strict'
import { cvView, matchesExperience, readResponse, streamChat, transcriptView } from '../src/experience.ts'

const sample = () => ({ ok: true, model: 'yolov11_n', original_size: [800, 400], result: { detections: [{ box: [80, 80, 480, 320], score: .9, class_id: 0 }] } })
test('draw only original-image XYXY coordinates with the matching model and size', () => {
  assert.deepEqual(cvView(sample(), 'yolov11_n', 800, 400).boxes[0].box, [80, 80, 480, 320])
  assert.equal(cvView(sample(), 'yolov11_n', 400, 800).boxes.length, 0)
  assert.equal(cvView(sample(), 'other', 800, 400).boxes.length, 0)
  assert.match(cvView(sample(), 'yolov11_n', 400, 800).warnings.join(), /尺寸/)
})
test('invalid numbers, inverted or wholly offscreen boxes never become plausible detections', () => {
  for (const box of [[NaN, 0, 30, 40], [true, 0, 30, 40], [1, 2, 1, 4], [900, 0, 1000, 10], ['0', 0, 20, 20]]) {
    const raw = sample(); raw.result.detections[0].box = box
    assert.equal(cvView(raw, raw.model, 800, 400).boxes.length, 0)
  }
  const raw = sample(); raw.result.detections[0].box = [-10, -20, 810, 450]
  assert.deepEqual(cvView(raw, raw.model, 800, 400).boxes[0].box, [0, 0, 800, 400])
  raw.result.detections[0].score = Infinity
  assert.equal(cvView(raw, raw.model, 800, 400).boxes.length, 0)
})
test('face landmarks, rotation/segmentation/tracking limits and no-target states stay explicit', () => {
  const raw = sample(); raw.result.faces = raw.result.detections; delete raw.result.detections
  raw.result.faces[0].landmarks = [[100, 100], [1000, 1000]]; raw.result.faces[0].angle_rad = .4
  raw.result.mask_note = 'metadata'; raw.result.tracking_note = 'detector only'
  const view = cvView(raw, raw.model, 800, 400)
  assert.equal(view.boxes[0].label, '人脸'); assert.equal(view.boxes[0].landmarks.length, 1); assert.equal(view.warnings.length, 3)
  raw.result.faces = []
  assert.equal(cvView(raw, raw.model, 800, 400).hasGeometry, true)
})
test('ASR timestamps are validated rather than fabricated', () => {
  assert.deepEqual(transcriptView({ text: 'hello' }), { text: 'hello', segments: [] })
  assert.throws(() => transcriptView({ text: 42 }))
  const view = transcriptView({ text: 'x', duration: 4, segments: [{ start: 0, end: 2, text: 'ok' }, { start: 0, end: 9, text: 'bad' }, { start: true, end: 2, text: 'bad' }] })
  assert.equal(view.segments.length, 1)
})
test('VLM metadata without vision boolean is still selectable; OCR remains distinct', () => {
  const vlm = { capability: 'chat', metadata: { type: 'vlm' } }
  assert.equal(matchesExperience(vlm, 'vision'), true)
  assert.equal(matchesExperience(vlm, 'chat'), false)
  assert.equal(matchesExperience({ ...vlm, metadata: { vision: true, type: 'document_ocr' } }, 'ocr'), true)
})
test('SSE handles split UTF8/CRLF, preserves text, and requires DONE', async () => {
  const encoded = new TextEncoder().encode('data: {"choices":[{"delta":{"content":"你好"}}]}\r\n\r\ndata: [DONE]\r\n\r\n')
  const body = new ReadableStream({ start(controller) { for (const byte of encoded) controller.enqueue(new Uint8Array([byte])); controller.close() } })
  let output = ''
  await streamChat(new Response(body), text => output += text, () => {})
  assert.equal(output, '你好')
  await assert.rejects(streamChat(new Response('data: {"choices":[]}\n\n'), () => {}, () => {}), /DONE/)
  await assert.rejects(streamChat(new Response('data: nope\n\n'), () => {}, () => {}))
})
test('binary response length is bounded', async () => {
  await assert.rejects(readResponse(new Response('12345'), 4), /过大/)
  assert.equal(new TextDecoder().decode(await readResponse(new Response('1234'), 4)), '1234')
})
