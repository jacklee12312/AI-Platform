import test from 'node:test'
import assert from 'node:assert/strict'
import { evaluationSelectionError, evaluationVerdict, latencyText, newEvaluationId } from '../src/evaluations.ts'

const run = () => ({ status: 'completed', verdict: 'passed', summary: { total: 1, passed: 1, failed: 0, skipped: 0, pending: 0 }, cases: [{ status: 'passed' }] })
const catalog = { suites: [{ id: 'smoke' }, { id: 'quality' }, { id: 'repeat' }], limits: { max_models: 23, max_repeats: 3 },
  models: [{ id: 'a', available: true, reason: null, suites: ['smoke', 'repeat'] }, { id: 'b', available: false, reason: '未启用', suites: ['smoke'] }] }

test('completed alone is not a passing evaluation', () => {
  assert.equal(evaluationVerdict(run()), 'passed')
  for (const status of ['cancelled', 'interrupted']) assert.equal(evaluationVerdict({ ...run(), status }), 'incomplete')
  assert.equal(evaluationVerdict({ ...run(), status: 'running' }), 'pending')
  assert.equal(evaluationVerdict({ ...run(), status: 'failed' }), 'failed')
  assert.equal(evaluationVerdict({ ...run(), cases: [] }), 'incomplete')
  assert.equal(evaluationVerdict({ ...run(), cases: [{ status: 'skipped' }] }), 'incomplete')
  assert.equal(evaluationVerdict({ ...run(), cases: [{ status: 'failed' }] }), 'failed')
})

test('model selection requires a fresh compatible available inventory and bounded repeats', () => {
  const plan = { models: ['a'], suite: 'smoke', repeats: 1 }
  assert.equal(evaluationSelectionError(catalog, plan), '')
  assert.notEqual(evaluationSelectionError(null, plan), '')
  for (const models of [[], ['a', 'a'], ['unknown'], ['b']]) assert.notEqual(evaluationSelectionError(catalog, { ...plan, models }), '')
  assert.notEqual(evaluationSelectionError(catalog, { ...plan, suite: 'quality' }), '')
  for (const repeats of [0, 1.1, 4, NaN, Infinity]) assert.notEqual(evaluationSelectionError(catalog, { ...plan, suite: 'repeat', repeats }), '')
  assert.notEqual(evaluationSelectionError(catalog, { ...plan, repeats: 2 }), '')
})

test('request IDs are stable-schema random IDs and unknown latency is never zero', () => {
  const first = newEvaluationId(), second = newEvaluationId()
  assert.match(first, /^[a-f0-9]{32}$/)
  assert.notEqual(first, second)
  assert.equal(latencyText(null), '—')
  assert.equal(latencyText(NaN), '—')
  assert.equal(latencyText(0), '0 ms')
})
