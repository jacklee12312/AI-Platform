import test from 'node:test'
import assert from 'node:assert/strict'
import { reactive } from 'vue'
import { defaultKeyPolicy, normalizePolicy } from '../src/keyPolicy.ts'
import { ApiError, describeError } from '../src/api.ts'

test('editing Vue policy preserves deny-all and null expiry without modifying the draft', () => {
  const draft = reactive({ ...defaultKeyPolicy(), allowed_models: [], daily_request_limit: '12' })
  const result = normalizePolicy(draft)
  assert.deepEqual(result.allowed_models, [])
  assert.equal(result.expires_at, null)
  assert.equal(result.daily_request_limit, 12)
  assert.equal(draft.daily_request_limit, '12')
})
test('invalid limits and expiry cannot be submitted by the form', () => {
  for (const n of [-1, 0.5, 1025, 'NaN']) assert.throws(() => normalizePolicy({ ...defaultKeyPolicy(), max_concurrent_requests: n }))
  assert.throws(() => normalizePolicy({ ...defaultKeyPolicy(), expires_at: 'bad-date' }))
})
test('proxied policy validation exposes the field and cause', () => {
  const error = new ApiError(422, { detail: [{ loc: ['body', 'policy', 'daily_request_limit'], msg: 'must be positive' }] })
  assert.match(describeError(error), /policy.daily_request_limit.*must be positive/)
})
