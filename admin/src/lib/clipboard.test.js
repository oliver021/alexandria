import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyText } from './clipboard.js'

afterEach(() => {
  vi.unstubAllGlobals()
  delete document.execCommand
})

describe('copyText', () => {
  it('uses the asynchronous clipboard API when it exists', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    await expect(copyText('alx_secret')).resolves.toBe(true)
    expect(writeText).toHaveBeenCalledWith('alx_secret')
  })

  it('falls back to execCommand when the clipboard API is missing', async () => {
    vi.stubGlobal('navigator', {})
    let copiedWhileSelected = ''
    document.execCommand = vi.fn(() => {
      const active = document.querySelector('textarea')
      copiedWhileSelected = active ? active.value : ''
      return true
    })
    await expect(copyText('hello')).resolves.toBe(true)
    expect(document.execCommand).toHaveBeenCalledWith('copy')
    expect(copiedWhileSelected).toBe('hello')
    expect(document.querySelector('textarea')).toBeNull()
  })

  it('falls back when the clipboard API rejects', async () => {
    vi.stubGlobal('navigator', {
      clipboard: { writeText: vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError')) },
    })
    document.execCommand = vi.fn(() => true)
    await expect(copyText('x')).resolves.toBe(true)
  })

  it('reports failure when nothing can copy', async () => {
    vi.stubGlobal('navigator', {})
    document.execCommand = vi.fn(() => false)
    await expect(copyText('x')).resolves.toBe(false)
    document.execCommand = vi.fn(() => {
      throw new Error('blocked')
    })
    await expect(copyText('x')).resolves.toBe(false)
    expect(document.querySelector('textarea')).toBeNull()
  })
})
