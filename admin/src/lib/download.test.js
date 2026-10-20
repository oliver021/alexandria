import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { saveBlob, sha256Hex, verifyDigest } from './download.js'

const HELLO_SHA = '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'

describe('saveBlob', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    URL.createObjectURL = vi.fn(() => 'blob:fake-url')
    URL.revokeObjectURL = vi.fn()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('downloads through an object URL with a sanitized name and cleans up', () => {
    const clicks = []
    const original = HTMLAnchorElement.prototype.click
    HTMLAnchorElement.prototype.click = function click() {
      clicks.push({ href: this.href, download: this.download, rel: this.rel })
    }
    try {
      saveBlob(new Blob(['x']), '../evil/name:1.zip')
    } finally {
      HTMLAnchorElement.prototype.click = original
    }
    expect(clicks).toEqual([{ href: 'blob:fake-url', download: '.._evil_name_1.zip', rel: 'noopener' }])
    expect(document.querySelector('a[download]')).toBeNull()
    expect(URL.revokeObjectURL).not.toHaveBeenCalled()
    vi.advanceTimersByTime(30_000)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:fake-url')
  })
})

describe('digests', () => {
  const blob = () => new Blob(['hello'])

  it('computes SHA-256', async () => {
    expect(await sha256Hex(blob())).toBe(HELLO_SHA)
  })

  it('verifies a matching digest, ignoring case', async () => {
    expect(await verifyDigest(blob(), HELLO_SHA)).toBe('match')
    expect(await verifyDigest(blob(), HELLO_SHA.toUpperCase())).toBe('match')
  })

  it('detects a mismatch', async () => {
    expect(await verifyDigest(blob(), 'f'.repeat(64))).toBe('mismatch')
  })

  it('reports an unverifiable download when no digest is announced', async () => {
    expect(await verifyDigest(blob(), null)).toBe('unverified')
    expect(await verifyDigest(blob(), '')).toBe('unverified')
  })

  it('reports an unverifiable download when WebCrypto is unavailable', async () => {
    vi.stubGlobal('crypto', {})
    expect(await verifyDigest(blob(), HELLO_SHA)).toBe('unverified')
    vi.unstubAllGlobals()
  })
})
