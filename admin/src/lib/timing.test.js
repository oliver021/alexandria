import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { debounce } from './timing.js'

describe('debounce', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('calls once with the latest arguments after the delay', () => {
    const spy = vi.fn()
    const run = debounce(spy, 300)
    run('a')
    vi.advanceTimersByTime(200)
    run('ab')
    vi.advanceTimersByTime(299)
    expect(spy).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy).toHaveBeenCalledWith('ab')
  })

  it('can be cancelled', () => {
    const spy = vi.fn()
    const run = debounce(spy, 100)
    run('x')
    run.cancel()
    vi.advanceTimersByTime(500)
    expect(spy).not.toHaveBeenCalled()
  })

  it('can be flushed', () => {
    const spy = vi.fn()
    const run = debounce(spy, 100)
    run('x')
    run.flush()
    expect(spy).toHaveBeenCalledWith('x')
    vi.advanceTimersByTime(500)
    expect(spy).toHaveBeenCalledTimes(1)
    run.flush()
    expect(spy).toHaveBeenCalledTimes(1)
  })
})
