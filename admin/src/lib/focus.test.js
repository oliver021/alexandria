import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetApp, scrolledIntoView } from '../test/helpers.js'
import { focusAndReveal, focusById, focusFirstInvalid } from './focus.js'

/**
 * @param {string} html
 * @returns {HTMLElement}
 */
function mount(html) {
  const host = document.createElement('div')
  host.innerHTML = html
  document.body.append(host)
  return host
}

/**
 * jsdom has no layout, so every element is 0px tall unless the test says otherwise.
 *
 * @param {Element} element
 * @param {number} height
 */
function giveHeight(element, height) {
  vi.spyOn(element, 'getBoundingClientRect').mockReturnValue(
    /** @type {DOMRect} */ ({ height, width: 300, top: 0, left: 0, right: 300, bottom: height, x: 0, y: 0, toJSON: () => ({}) }),
  )
}

beforeEach(() => {
  resetApp()
  vi.stubGlobal('innerHeight', 800)
})

afterEach(() => {
  document.body.innerHTML = ''
  resetApp()
})

describe('focusById', () => {
  it('focuses the element once the pending updates are in the DOM', async () => {
    mount('<div id="target" tabindex="-1"></div>')
    await focusById('target')
    expect(document.getElementById('target')).toHaveFocus()
  })

  it('does nothing when the page has no such element', async () => {
    mount('<button id="other">Other</button>')
    document.getElementById('other')?.focus()
    await focusById('missing')
    expect(document.getElementById('other')).toHaveFocus()
  })
})

describe('focusAndReveal', () => {
  it('moves the focus to the element and scrolls it to the middle of the window', async () => {
    mount('<div id="failure" tabindex="-1">The upload failed</div>')
    const element = /** @type {HTMLElement} */ (document.getElementById('failure'))
    giveHeight(element, 170)
    expect(await focusAndReveal('failure')).toBe(true)
    expect(element).toHaveFocus()
    expect(scrolledIntoView()).toEqual([
      { element, options: { block: 'center', inline: 'nearest' } },
    ])
  })

  it('focuses without scrolling, so the browser does not scroll twice', async () => {
    mount('<div id="failure" tabindex="-1"></div>')
    const element = /** @type {HTMLElement} */ (document.getElementById('failure'))
    const focus = vi.spyOn(element, 'focus')
    await focusAndReveal('failure')
    expect(focus).toHaveBeenCalledExactlyOnceWith({ preventScroll: true })
  })

  describe('when the sticky header covers the top of the window', () => {
    // layout.css sets scroll-padding-top to the height of the header; jsdom has no stylesheet.
    beforeEach(() => {
      vi.stubGlobal('getComputedStyle', () => ({ scrollPaddingTop: '104px' }))
    })

    it.each([
      ['fits in what the header leaves visible', 696, 'center'],
      ['is taller than what the header leaves visible', 697, 'start'],
      ['fills the whole window', 800, 'start'],
      ['is a short message', 170, 'center'],
    ])('aligns an element that %s (%ipx of 696px) to the %s', async (_name, height, block) => {
      mount('<div id="message" tabindex="-1"></div>')
      const element = /** @type {HTMLElement} */ (document.getElementById('message'))
      giveHeight(element, height)
      await focusAndReveal('message')
      expect(scrolledIntoView()[0].options).toEqual({ block, inline: 'nearest' })
    })
  })

  it('uses the whole window when nothing covers the top of it', async () => {
    mount('<div id="message" tabindex="-1"></div>')
    const element = /** @type {HTMLElement} */ (document.getElementById('message'))
    giveHeight(element, 750)
    await focusAndReveal('message')
    expect(scrolledIntoView()[0].options).toEqual({ block: 'center', inline: 'nearest' })
  })

  it('does nothing, and says so, when the page has no such element', async () => {
    expect(await focusAndReveal('missing')).toBe(false)
    expect(scrolledIntoView()).toEqual([])
  })
})

describe('focusFirstInvalid', () => {
  it('focuses the first control that is marked invalid and brings it into view', async () => {
    const host = mount(`
      <form>
        <input id="a" aria-invalid="false" />
        <input id="b" aria-invalid="true" />
        <input id="c" aria-invalid="true" />
      </form>`)
    const form = /** @type {HTMLFormElement} */ (host.querySelector('form'))
    expect(await focusFirstInvalid(form)).toBe(true)
    const second = document.getElementById('b')
    expect(second).toHaveFocus()
    expect(scrolledIntoView().map((item) => item.element)).toEqual([second])
  })

  it('does nothing when every control is valid', async () => {
    const host = mount('<form><input aria-invalid="false" /><input /></form>')
    expect(await focusFirstInvalid(host.querySelector('form'))).toBe(false)
    expect(document.body).toHaveFocus()
    expect(scrolledIntoView()).toEqual([])
  })

  it('copes with a form that is not mounted yet', async () => {
    expect(await focusFirstInvalid(undefined)).toBe(false)
    expect(await focusFirstInvalid(null)).toBe(false)
  })
})
