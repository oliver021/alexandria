import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

// jsdom draws nothing, so it cannot say how a page is laid out. `browser/console.test.js` measures the
// layout in a real Chrome, and skips when there is none. These checks read the rules that make that
// layout work, so that removing one is noticed on every machine.

const STYLES = join(import.meta.dirname, 'styles')

/**
 * The declarations of a rule that starts at the beginning of a line (so not one inside `@media`).
 *
 * @param {string} file
 * @param {string} selector
 * @returns {Record<string, string>}
 */
function rule(file, selector) {
  const css = readFileSync(join(STYLES, file), 'utf8')
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = css.match(new RegExp(`^${escaped}\\s*\\{([^}]*)\\}`, 'm'))
  expect(match, `${selector} in ${file}`).not.toBeNull()
  return Object.fromEntries(
    /** @type {RegExpMatchArray} */ (match)[1]
      .split(';')
      .map((declaration) => declaration.trim())
      .filter(Boolean)
      .map((declaration) => {
        const colon = declaration.indexOf(':')
        return [declaration.slice(0, colon).trim(), declaration.slice(colon + 1).trim()]
      }),
  )
}

describe('the rules behind the layout', () => {
  it('sizes the key column of the compact list by its longest key, but never past 40% of the list', () => {
    // `max-content` here let one key of 40 characters take the whole row, and the values got 18px.
    expect(rule('pages.css', '.dl-compact')['grid-template-columns']).toBe('fit-content(40%) minmax(0, 1fr)')
  })

  it('lets a key of any list break anywhere, so a key without a space cannot widen the page', () => {
    const key = rule('components.css', '.dl dt')
    expect(key['min-width']).toBe('0')
    expect(key['overflow-wrap']).toBe('anywhere')
  })

  it('keeps what the browser scrolls to below the sticky header', () => {
    // 3.5rem is the header on one row; the extra 3rem covers its navigation wrapped onto a second row.
    expect(rule('layout.css', 'html')['scroll-padding-top']).toBe('calc(var(--header-height) + 3rem)')
    expect(rule('tokens.css', ':root')['--header-height']).toBe('3.5rem')
  })
})
