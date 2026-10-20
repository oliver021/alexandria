import { describe, expect, it } from 'vitest'
import { ApiError } from './api.js'
import { createResource } from './resource.svelte.js'

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe('createResource', () => {
  it('loads data and tracks the loading flag', async () => {
    const resource = createResource(async (_signal, id) => ({ id }))
    expect(resource.loaded).toBe(false)
    const pending = resource.load(7)
    expect(resource.loading).toBe(true)
    await pending
    expect(resource.loading).toBe(false)
    expect(resource.loaded).toBe(true)
    expect(resource.data).toEqual({ id: 7 })
    expect(resource.error).toBeNull()
  })

  it('lets only the latest request win', async () => {
    const first = deferred()
    const second = deferred()
    const queue = [first, second]
    const resource = createResource(() => queue.shift().promise)
    const a = resource.load()
    const b = resource.load()
    second.resolve('second')
    await b
    first.resolve('first')
    await a
    expect(resource.data).toBe('second')
    expect(resource.loading).toBe(false)
  })

  it('aborts the previous request when a new one starts', async () => {
    const signals = []
    const resource = createResource((signal) => {
      signals.push(signal)
      return new Promise(() => {})
    })
    resource.load()
    resource.load()
    expect(signals[0].aborted).toBe(true)
    expect(signals[1].aborted).toBe(false)
    resource.abort()
    expect(signals[1].aborted).toBe(true)
    expect(resource.loading).toBe(false)
  })

  it('stores errors and clears them on the next success', async () => {
    let fail = true
    const resource = createResource(async () => {
      if (fail) throw new ApiError({ status: 500, code: 'INTERNAL_ERROR', message: 'boom' })
      return 'ok'
    })
    await resource.load()
    expect(resource.error).toMatchObject({ code: 'INTERNAL_ERROR' })
    expect(resource.loaded).toBe(false)
    fail = false
    await resource.reload()
    expect(resource.error).toBeNull()
    expect(resource.data).toBe('ok')
  })

  it('keeps stale data when a refresh fails', async () => {
    let fail = false
    const resource = createResource(async () => {
      if (fail) throw new ApiError({ status: 500, code: 'INTERNAL_ERROR', message: 'boom' })
      return 'first'
    })
    await resource.load()
    fail = true
    await resource.reload()
    expect(resource.data).toBe('first')
    expect(resource.error).toBeTruthy()
  })

  it('ignores aborts and 401 responses, which are handled by the session', async () => {
    const resource = createResource(async () => {
      throw new DOMException('aborted', 'AbortError')
    })
    await resource.load()
    expect(resource.error).toBeNull()
    const unauthorized = createResource(async () => {
      throw new ApiError({ status: 401, code: 'UNAUTHORIZED', message: 'expired' })
    })
    await unauthorized.load()
    expect(unauthorized.error).toBeNull()
  })

  it('reloads with the previous arguments', async () => {
    const calls = []
    const resource = createResource(async (_signal, a, b) => {
      calls.push([a, b])
      return a + b
    })
    await resource.load(1, 2)
    await resource.reload()
    expect(calls).toEqual([
      [1, 2],
      [1, 2],
    ])
  })

  it('lets callers set the data directly', () => {
    const resource = createResource(async () => 'x')
    resource.set('manual')
    expect(resource.data).toBe('manual')
    expect(resource.loaded).toBe(true)
  })
})
