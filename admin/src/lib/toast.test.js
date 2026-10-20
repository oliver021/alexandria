import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toasts } from './toast.svelte.js'

beforeEach(() => {
  vi.useFakeTimers()
  toasts.clear()
})

afterEach(() => {
  toasts.clear()
  vi.useRealTimers()
})

describe('toasts', () => {
  it('queues messages with their tone', () => {
    toasts.success('Saved')
    toasts.error('Broke')
    expect(toasts.items.map((toast) => [toast.tone, toast.message])).toEqual([
      ['success', 'Saved'],
      ['error', 'Broke'],
    ])
  })

  it('dismisses success messages by themselves', () => {
    toasts.success('Saved')
    vi.advanceTimersByTime(5000)
    expect(toasts.items).toHaveLength(0)
  })

  it('keeps errors until they are dismissed', () => {
    const id = toasts.error('Broke')
    vi.advanceTimersByTime(60_000)
    expect(toasts.items).toHaveLength(1)
    toasts.dismiss(id)
    expect(toasts.items).toHaveLength(0)
  })

  it('assigns unique identifiers and dismisses only the requested one', () => {
    const first = toasts.info('one')
    const second = toasts.info('two')
    expect(first).not.toBe(second)
    toasts.dismiss(first)
    expect(toasts.items.map((toast) => toast.message)).toEqual(['two'])
  })

  it('honours a custom timeout and ignores unknown identifiers', () => {
    toasts.show('quick', { tone: 'warning', timeout: 100 })
    toasts.dismiss(999)
    vi.advanceTimersByTime(100)
    expect(toasts.items).toHaveLength(0)
  })
})
