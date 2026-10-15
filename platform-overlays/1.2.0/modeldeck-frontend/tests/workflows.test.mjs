import test from 'node:test'
import assert from 'node:assert/strict'
import { createJobTracker } from '../src/jobTracker.ts'
import { tokenText, usageQuality, dateBoundaries } from '../src/usagePresentation.ts'

function fixture(responses, saved) {
  const state = { busy: false, querying: false, label: '', job: null, error: '' }
  const calls = []
  let stored = saved || null
  const storage = { getItem: () => stored, setItem: (_, value) => { stored = value }, removeItem: () => { stored = null } }
  const request = async (path, init) => {
    calls.push({ path, method: init?.method || 'GET' })
    const next = responses.shift()
    if (next instanceof Error) throw next
    return next
  }
  return { state, calls, storage, tracker: createJobTracker(state, request, storage, async () => {}) }
}
test('queued/running/succeeded are distinct, no premature success', async () => {
  const f = fixture([{ id:'job', status:'queued' }, { id:'job', status:'running' }, { id:'job', status:'succeeded' }])
  const pending = f.tracker.submit('/restart', { method:'POST' }, 'restart')
  assert.equal(f.state.busy, true)
  await assert.rejects(f.tracker.submit('/restart', { method:'POST' }, 'duplicate'))
  await pending
  assert.equal(f.calls.filter(c => c.method === 'POST').length, 1)
  assert.equal(f.state.busy, false)
  assert.equal(f.storage.getItem(), null)
})
test('backend failure clears lock and exposes error', async () => {
  const f = fixture([{id:'job'}, {id:'job', status:'failed', error:'health timeout'}])
  await assert.rejects(f.tracker.submit('/restart', {method:'POST'}, 'restart'), /health timeout/)
  assert.equal(f.state.busy, false)
  assert.match(f.state.error, /health timeout/)
})
test('network loss keeps operation locked; refresh only queries existing task', async () => {
  const f = fixture([{id:'job'}, new Error('offline')])
  await assert.rejects(f.tracker.submit('/restart', {method:'POST'}, 'restart'), /offline/)
  assert.equal(f.state.busy, true)
  const resumed = fixture([{id:'job',status:'succeeded'}], f.storage.getItem())
  await resumed.tracker.resume()
  assert.deepEqual(resumed.calls, [{path:'/api/jobs/job',method:'GET'}])
})
test('unknown result never auto-resubmits and requires acknowledgement', async () => {
  const f = fixture([{id:'job'}, {id:'job',status:'unknown'}])
  await assert.rejects(f.tracker.submit('/restart', {method:'POST'}, 'restart'))
  assert.equal(f.state.busy, true)
  f.tracker.acknowledge()
  assert.equal(f.state.busy, false)
})
test('ambiguous submission versus explicit 4xx denial', async () => {
  for (const status of [undefined, 500, 409]) {
    const f = fixture([Object.assign(new Error('denied'), {status})])
    await assert.rejects(f.tracker.submit('/restart', {method:'POST'}, 'restart'))
    assert.equal(f.state.busy, status !== 409)
  }
})
test('null unknown differs from genuine 0 and usage anomaly has explicit label', () => {
  assert.equal(tokenText(null), '未知')
  assert.equal(tokenText(0), '0')
  assert.equal(usageQuality('inconsistent'), '计数不一致')
})
test('date range rejects malformed/impossible/reversed dates', () => {
  assert.throws(() => dateBoundaries('2026-02-30', ''))
  assert.throws(() => dateBoundaries('2026-09-05', '2026-09-04'))
  const range = dateBoundaries('2026-09-04', '2026-09-04')
  assert.equal(Date.parse(range.end) - Date.parse(range.start), 86400000 - 1)
})
