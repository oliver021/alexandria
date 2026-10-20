import { describe, expect, it } from 'vitest'
import { FRAMED_MESSAGE, isFramed } from './framing.js'

describe('isFramed', () => {
  it('is false for the page of a tab, which is its own top window', () => {
    expect(window.top).toBe(window.self)
    expect(isFramed()).toBe(false)
  })

  it('is false when the top window is the window itself', () => {
    const self = {}
    expect(isFramed({ top: self, self })).toBe(false)
  })

  it('is true when the top window is another window', () => {
    expect(isFramed({ top: {}, self: {} })).toBe(true)
  })

  it('is true inside a real frame', () => {
    const frame = document.createElement('iframe')
    document.body.append(frame)
    try {
      expect(frame.contentWindow).not.toBeNull()
      expect(isFramed(/** @type {Window} */ (frame.contentWindow))).toBe(true)
      expect(isFramed()).toBe(false)
    } finally {
      frame.remove()
    }
  })

  it('fails closed when the top window cannot be read at all', () => {
    const hostile = {
      self: {},
      get top() {
        throw new DOMException('Blocked a frame from accessing a cross-origin frame', 'SecurityError')
      },
    }
    expect(isFramed(hostile)).toBe(true)
  })
})

describe('FRAMED_MESSAGE', () => {
  it('tells the person what to do instead', () => {
    expect(FRAMED_MESSAGE).toMatch(/cannot be shown inside another page/)
    expect(FRAMED_MESSAGE).toMatch(/own tab/)
  })
})
