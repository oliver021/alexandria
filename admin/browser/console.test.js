// @vitest-environment node
// What jsdom cannot show: how the built console looks and scrolls in a real browser.
//
// It builds the console, hosts it the way the registry has to (hosting.js), drives a headless Chrome
// and answers the calls of the console with canned data. The tests skip when there is no Chrome, or when
// ALEXANDRIA_NO_BROWSER is set, and fail when Chrome is there but does not work.

import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { findChrome, launchChrome } from './chrome.js'
import { buildConsole, sendJson, serveConsole } from './console-server.js'

// Starting Chrome and building the console take longer than a test of a component. This file only.
vi.setConfig({ testTimeout: 60_000, hookTimeout: 120_000 })

const available = Boolean(findChrome()) && !process.env.ALEXANDRIA_NO_BROWSER

const ADMIN = {
  id: 'user-admin-0001',
  username: 'ada',
  email: 'ada@example.test',
  role: 'admin',
  is_active: true,
  created_at: '2026-01-01T10:00:00Z',
}
const SESSION = { token: 'test-session-token', user: ADMIN }

// Everything below the front matter of a package is chosen by whoever publishes it.
const LONG_KEY = 'x-this-key-is-chosen-by-the-publisher-of-the-package-0123456' // 60 characters, with places to break it
const UNBROKEN_KEY = 'k'.repeat(80)
const METADATA = {
  author: 'Ada Lovelace',
  homepage: `https://example.test/${'a/very/long/path/'.repeat(8)}`,
  summary: 'Line one of a value.\nLine two of a value.',
  [LONG_KEY]: 'A perfectly normal value for a very long key.',
  [UNBROKEN_KEY]: 'A value for a key without a single place to break it.',
}

/**
 * @param {number} count
 */
function versionsOf(count) {
  return Array.from({ length: count }, (_, index) => {
    const number = count - index
    return {
      id: `version-${number}`,
      skill_id: 'entry-1',
      version: `1.${number}.0`,
      status: number === count ? 'published' : 'draft',
      sha256: String(number).repeat(64).slice(0, 64),
      size_bytes: 2048,
      uncompressed_bytes: 4096,
      file_count: 3,
      entrypoint: 'SKILL.md',
      display_name: 'Probe',
      description: 'A probe.',
      category: 'testing',
      tags: ['probe'],
      uploaded_by: ADMIN.id,
      published_by: number === count ? ADMIN.id : null,
      published_at: number === count ? '2026-03-01T10:00:00Z' : null,
      deprecated_at: null,
      created_at: '2026-03-01T09:00:00Z',
    }
  })
}

const ENTRY = {
  id: 'entry-1',
  name: 'xss-probe',
  display_name: 'Probe for hostile metadata',
  description: 'A package that checks how the console draws data that the publisher controls.',
  kind: 'skill',
  category: 'testing',
  tags: ['probe'],
  status: 'published',
  latest_version: '1.30.0',
  owner_id: ADMIN.id,
  created_at: '2026-02-01T10:00:00Z',
  updated_at: '2026-03-01T10:00:00Z',
  versions: versionsOf(30),
}

const LIMITS = {
  max_package_bytes: 26214400,
  max_uncompressed_bytes: 104857600,
  max_file_bytes: 52428800,
  max_files: 2000,
  max_inline_read_bytes: 262144,
}

/**
 * @param {import('node:http').IncomingMessage} request
 */
function drain(request) {
  return new Promise((resolve) => {
    request.on('end', () => resolve(undefined))
    request.resume()
  })
}

/**
 * The canned answers of the registry. Every write is refused, which is what these tests look at.
 *
 * @type {import('./console-server.js').ApiHandler}
 */
async function registry(request, response, url) {
  const path = url.pathname.replace('/api/v1', '')
  const key = `${request.method} ${path}`
  const refuse = (/** @type {number} */ status, /** @type {string} */ code, /** @type {string} */ message, reason = undefined) =>
    sendJson(response, status, { error: { code, message, ...(reason ? { reason } : {}) } })

  if (request.method !== 'GET') await drain(request)
  if (key === 'GET /auth/me') sendJson(response, 200, ADMIN)
  else if (key === 'POST /auth/login') refuse(401, 'UNAUTHORIZED', 'Invalid credentials')
  else if (key === 'GET /info') {
    sendJson(response, 200, { name: 'Alexandria', version: '0.1.0', public_read: true, public_url: '', mcp_path: '/mcp', limits: LIMITS })
  } else if (key === 'POST /packages') {
    refuse(422, 'INVALID_PACKAGE', 'The package must contain SKILL.md or INDEX.md at its root', 'MISSING_ENTRYPOINT')
  } else if (key === 'GET /users') sendJson(response, 200, [ADMIN])
  else if (key === 'GET /openapi.json') sendJson(response, 200, { openapi: '3.1.0', paths: {} })
  else if (key === 'POST /users') refuse(409, 'CONFLICT', "Username 'margaret' already exists")
  else if (key === 'GET /tokens') sendJson(response, 200, [])
  else if (key === 'POST /tokens') refuse(409, 'CONFLICT', 'The limit of 20 active API tokens per user has been reached')
  else if (key === 'GET /skills/xss-probe') sendJson(response, 200, ENTRY)
  else if (/^GET \/skills\/xss-probe\/versions\/[^/]+$/.test(key)) {
    sendJson(response, 200, { ...ENTRY.versions[0], metadata: METADATA, download_url: 'http://127.0.0.1/download' })
  } else if (/^POST \/skills\/xss-probe\/versions\/[^/]+\/publish$/.test(key)) {
    refuse(403, 'FORBIDDEN', 'Only the owner of this entry or an admin can change it')
  } else return false
  return true
}

/** @type {Awaited<ReturnType<typeof launchChrome>>} */
let browser
/** @type {Awaited<ReturnType<typeof serveConsole>>} */
let site
let work = ''
/** @type {import('./chrome.js').Page[]} */
const opened = []

beforeAll(async () => {
  if (!available) return
  work = mkdtempSync(join(tmpdir(), 'alexandria-console-'))
  await buildConsole(join(work, 'dist'))
  site = await serveConsole({ root: join(work, 'dist'), api: registry })
  browser = await launchChrome()
  writeFileSync(join(work, 'no-entrypoint.zip'), 'PK')
})

afterEach(async () => {
  await Promise.all(opened.splice(0).map((page) => page.close()))
})

afterAll(async () => {
  await browser?.close()
  await site?.close()
  if (work) rmSync(work, { recursive: true, force: true })
})

/**
 * @param {string} hash where to go inside the console
 * @param {{ width: number, height: number }} size of the window
 * @param {{ signedIn?: boolean, origin?: string, path?: string }} [options] `origin` and `path` open
 *   another server, or a page that is not the console
 */
async function open(hash, size, { signedIn = true, origin = site.origin, path = '/admin/' } = {}) {
  const page = await browser.newPage(size)
  opened.push(page)
  if (signedIn) {
    await page.runBeforeLoad(`sessionStorage.setItem('alexandria.admin.session', ${JSON.stringify(JSON.stringify(SESSION))})`)
  }
  await page.goto(`${origin}${path}${hash}`)
  return page
}

/**
 * Types into fields the way a form sees it: a value and an input event.
 *
 * @param {import('./chrome.js').Page} page
 * @param {Record<string, string>} fields by id
 */
async function fill(page, fields) {
  await page.evaluate((values) => {
    for (const [id, value] of Object.entries(values)) {
      const input = /** @type {HTMLInputElement} */ (document.getElementById(id))
      input.value = value
      input.dispatchEvent(new Event('input', { bubbles: true }))
    }
  }, fields)
}

/**
 * Scrolls until the element sits just below the header, or as near to that as the page allows: the
 * position in which a person who scrolled down to the button presses it.
 *
 * @param {import('./chrome.js').Page} page
 * @param {string} selector
 */
async function scrollUnderHeader(page, selector) {
  await page.evaluate((css) => {
    const element = /** @type {HTMLElement} */ (document.querySelector(css))
    const header = document.querySelector('.app-header')?.getBoundingClientRect().height ?? 0
    window.scrollTo(0, window.scrollY + element.getBoundingClientRect().top - header - 8)
  }, selector)
}

/**
 * Where an element is, compared with the window and the sticky header.
 *
 * @param {import('./chrome.js').Page} page
 * @param {string} selector
 */
function whereIs(page, selector) {
  return page.evaluate((css) => {
    const element = /** @type {HTMLElement} */ (document.querySelector(css))
    const box = element.getBoundingClientRect()
    const header = document.querySelector('.app-header')?.getBoundingClientRect().bottom ?? 0
    return {
      top: Math.round(box.top),
      bottom: Math.round(box.bottom),
      height: Math.round(box.height),
      headerBottom: Math.round(header),
      windowHeight: innerHeight,
      focusedId: document.activeElement?.id || document.activeElement?.tagName,
    }
  }, selector)
}

/**
 * The element is in the window, and not under the header. When it cannot fit, its start is.
 *
 * @param {Awaited<ReturnType<typeof whereIs>>} where
 */
function expectOnScreen(where) {
  expect(where.top, 'the top of it is under the header or above the window').toBeGreaterThanOrEqual(where.headerBottom)
  expect(where.top, 'the top of it is below the window').toBeLessThan(where.windowHeight)
  if (where.height <= where.windowHeight - where.headerBottom) {
    expect(where.bottom, 'the bottom of it is below the window').toBeLessThanOrEqual(where.windowHeight)
  }
}

const SIZES = [
  { name: 'a laptop', width: 1366, height: 680 },
  { name: 'a short laptop window', width: 1366, height: 500 },
  { name: 'a phone', width: 375, height: 812 },
  { name: 'a small phone', width: 320, height: 568 },
  { name: 'a phone on its side', width: 667, height: 300 },
]

describe.skipIf(!available)('the console in a real browser', () => {
  it('starts under the headers of the hosting contract without a single complaint of the browser', async () => {
    const page = await open('#/library/xss-probe', { width: 1280, height: 800 })
    await page.waitFor(() => document.querySelector('h1')?.textContent === 'Probe for hostile metadata')
    expect(page.problems).toEqual([])
  })

  it('would hear a complaint, so that a quiet page means something', async () => {
    const page = await open('#/login', { width: 800, height: 600 }, { signedIn: false })
    await page.waitFor(() => !!document.getElementById('login-username'))
    expect(page.problems).toEqual([])
    // The policy forbids inline scripts, which is what a page that breaks it would try.
    await page.evaluate(() => {
      const script = document.createElement('script')
      script.textContent = 'window.inlineScriptRan = true'
      document.head.append(script)
    })
    await vi.waitFor(() => expect(page.problems.join('\n')).toMatch(/Content Security Policy/i))
    expect(await page.evaluate(() => 'inlineScriptRan' in window)).toBe(false)
  })

  describe('inside a frame of another page', () => {
    const PARENT = '<!doctype html><iframe id="frame" src="/admin/#/login" width="600" height="400"></iframe>'

    it('is refused by the browser when the registry sends the headers of the hosting contract', async () => {
      const framing = await serveConsole({ root: join(work, 'dist'), api: registry, pages: { '/outside.html': PARENT } })
      try {
        const page = await open('', { width: 800, height: 600 }, { origin: framing.origin, path: '/outside.html' })
        const frame = await vi.waitFor(async () => {
          const { frameTree } = await page.send('Page.getFrameTree')
          const child = frameTree.childFrames?.[0]?.frame
          expect(child).toBeDefined()
          return child
        })
        // The browser did not load the console: it put an error page where it should have been.
        expect(frame.unreachableUrl).toContain('/admin/')
        expect(frame.url).toMatch(/^chrome-error:/)
      } finally {
        await framing.close()
      }
    })

    it('shows only a message, and asks the registry for nothing, when the headers are missing', async () => {
      const plain = await serveConsole({
        root: join(work, 'dist'),
        api: registry,
        hosting: false,
        pages: { '/outside.html': PARENT },
      })
      try {
        const page = await open('', { width: 800, height: 600 }, { origin: plain.origin, path: '/outside.html' })
        const shown = await page.waitFor(() => {
          const frame = /** @type {HTMLIFrameElement} */ (document.getElementById('frame'))
          return frame.contentDocument?.getElementById('app')?.textContent
        })
        expect(shown).toBe('Alexandria Admin cannot be shown inside another page. Open it in its own tab.')
        const inFrame = await page.evaluate(() => {
          const frame = /** @type {HTMLIFrameElement} */ (document.getElementById('frame'))
          return frame.contentDocument?.querySelectorAll('button, input, form, nav').length
        })
        expect(inFrame).toBe(0)
        // The frame had a session, and still nothing was asked of the API.
        expect(plain.requests.filter((request) => request.includes('/api/'))).toEqual([])
      } finally {
        await plain.close()
      }
    })
  })

  describe('the Metadata card, whose keys are chosen by the publisher of the package', () => {
    /**
     * @param {import('./chrome.js').Page} page
     */
    async function measure(page) {
      await page.waitFor(() => document.querySelectorAll('.dl-compact dt').length === 5)
      return page.evaluate(() => {
        const list = /** @type {HTMLElement} */ (document.querySelector('.dl-compact'))
        const widths = (/** @type {string} */ tag) =>
          [...list.querySelectorAll(tag)].map((element) => element.getBoundingClientRect().width)
        return {
          list: list.getBoundingClientRect().width,
          keys: widths('dt'),
          values: widths('dd'),
          card: /** @type {HTMLElement} */ (list.closest('.card')).getBoundingClientRect().height,
          window: innerHeight,
          page: document.documentElement.scrollWidth,
          width: innerWidth,
        }
      })
    }

    it('gives a long key at most 40% of the card, so the values stay readable', async () => {
      const page = await open('#/library/xss-probe', { width: 1280, height: 800 })
      const sizes = await measure(page)
      expect(Math.max(...sizes.keys)).toBeLessThanOrEqual(sizes.list * 0.4 + 1)
      expect(Math.min(...sizes.values)).toBeGreaterThanOrEqual(150)
      // Before the fix a key of 40 characters left the values 18px: one character per line.
      expect(sizes.card).toBeLessThan(sizes.window * 1.5)
      expect(sizes.page).toBeLessThanOrEqual(sizes.width)
    })

    it('does the same in the narrower two-column layout', async () => {
      const page = await open('#/library/xss-probe', { width: 1024, height: 768 })
      const sizes = await measure(page)
      expect(Math.max(...sizes.keys)).toBeLessThanOrEqual(sizes.list * 0.4 + 1)
      expect(Math.min(...sizes.values)).toBeGreaterThanOrEqual(120)
      expect(sizes.card).toBeLessThan(sizes.window * 1.5)
    })

    it('stacks keys and values on a phone, with no sideways scrolling', async () => {
      const page = await open('#/library/xss-probe', { width: 375, height: 812 })
      const sizes = await measure(page)
      expect(Math.min(...sizes.keys, ...sizes.values)).toBeGreaterThanOrEqual(sizes.list - 1)
      expect(sizes.page).toBeLessThanOrEqual(sizes.width)
    })
  })

  describe.each(SIZES)('on $name ($width x $height)', ({ width, height }) => {
    const size = { width, height }

    it('shows why an upload failed, right where the person is looking', async () => {
      const page = await open('#/upload', size)
      await page.waitFor(() => !!document.getElementById('upload-file'))
      await page.chooseFiles('#upload-file', [join(work, 'no-entrypoint.zip')])
      await page.waitFor(() => [...document.querySelectorAll('button[type=submit]')].some((button) => !button.disabled))
      await scrollUnderHeader(page, 'form button[type=submit]')
      await page.click('form button[type=submit]')
      await page.waitFor(() => !!document.getElementById('upload-failure'))
      const where = await whereIs(page, '#upload-failure')
      expectOnScreen(where)
      expect(where.focusedId).toBe('upload-failure')
    })

    it('shows what is wrong with the user form, at the first field that needs attention', async () => {
      const page = await open('#/users', size)
      await page.waitFor(() => !!document.getElementById('user-username'))
      await scrollUnderHeader(page, 'form button[type=submit]')
      await page.click('form button[type=submit]')
      await page.waitFor(() => !!document.getElementById('user-username-error'))
      const where = await whereIs(page, '#user-username')
      expectOnScreen(where)
      expect(where.focusedId).toBe('user-username')
    })

    it('shows why a user was refused', async () => {
      const page = await open('#/users', size)
      await page.waitFor(() => !!document.getElementById('user-username'))
      await fill(page, {
        'user-username': 'margaret',
        'user-email': 'margaret@example.test',
        'user-password': 'correct horse',
      })
      await scrollUnderHeader(page, 'form button[type=submit]')
      await page.click('form button[type=submit]')
      await page.waitFor(() => !!document.getElementById('user-create-error'))
      const where = await whereIs(page, '#user-create-error')
      expectOnScreen(where)
      expect(where.focusedId).toBe('user-create-error')
    })

    it('shows what is wrong with the token form, at the first field that needs attention', async () => {
      const page = await open('#/tokens', size)
      await page.waitFor(() => !!document.getElementById('token-name'))
      await scrollUnderHeader(page, 'form button[type=submit]')
      await page.click('form button[type=submit]')
      await page.waitFor(() => !!document.getElementById('token-name-error'))
      const where = await whereIs(page, '#token-name')
      expectOnScreen(where)
      expect(where.focusedId).toBe('token-name')
    })

    it('shows why signing in failed', async () => {
      const page = await open('#/login', size, { signedIn: false })
      await page.waitFor(() => !!document.getElementById('login-username'))
      await fill(page, { 'login-username': 'ada', 'login-password': 'wrong password' })
      await scrollUnderHeader(page, 'form button[type=submit]')
      await page.click('form button[type=submit]')
      await page.waitFor(() => !!document.getElementById('login-error'))
      const where = await whereIs(page, '#login-error')
      expectOnScreen(where)
      expect(where.focusedId).toBe('login-error')
    })

    it('shows why a version could not be published, although its button is far down the table', async () => {
      const page = await open('#/library/xss-probe?tab=versions', size)
      await page.waitFor(() => document.querySelectorAll('tbody tr').length === 30)
      const target = 'tbody tr:nth-last-child(2) .btn-primary'
      await page.evaluate((css) => document.querySelector(css)?.scrollIntoView({ block: 'center' }), target)
      await page.click(target)
      await page.waitFor(() => !!document.querySelector('dialog[open]'))
      await page.click('dialog[open] .dialog__actions .btn-primary')
      await page.waitFor(() => !!document.getElementById('versions-notice'))
      const where = await whereIs(page, '#versions-notice')
      expectOnScreen(where)
      expect(where.focusedId).toBe('versions-notice')
    })
  })
})
