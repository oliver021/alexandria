// @vitest-environment node
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterAll, beforeAll, describe, expect, it } from 'vitest'
import { serveConsole } from '../browser/console-server.js'
import { CONTENT_SECURITY_POLICY } from '../csp.js'
import {
  IMMUTABLE,
  REVALIDATE,
  SECURITY_HEADERS,
  expectedCacheControl,
  expectedHeaders,
  isConsolePath,
} from '../hosting.js'
import { checkHosting, headerProblems, report } from './check-hosting.mjs'

describe('the hosting contract', () => {
  it.each([
    ['/admin', true],
    ['/admin/', true],
    ['/admin/index.html', true],
    ['/admin/assets/index-abc.js', true],
    ['/administrator', false],
    ['/api/v1/admin', false],
    ['/registry', false],
    ['/', false],
  ])('applies to %s: %s', (path, expected) => {
    expect(isConsolePath(path)).toBe(expected)
  })

  it('caches hashed files for a year, but only the ones that exist', () => {
    const asset = '/admin/assets/index-abc12345.js'
    expect(expectedCacheControl(asset, 200)).toBe(IMMUTABLE)
    expect(expectedCacheControl(asset, 304)).toBe(IMMUTABLE)
    for (const status of [206, 301, 403, 404, 500]) {
      expect(expectedCacheControl(asset, status), String(status)).toBe(REVALIDATE)
    }
  })

  it('makes the browser ask again for the page, which names the hashed files of one build', () => {
    for (const path of ['/admin', '/admin/', '/admin/index.html', '/admin/favicon.svg']) {
      expect(expectedCacheControl(path, 200), path).toBe(REVALIDATE)
    }
    expect(expectedCacheControl('/admin/assets', 200)).toBe(REVALIDATE)
  })

  it('asks for the same four security headers on every response of the console, errors included', () => {
    for (const status of [200, 304, 307, 404]) {
      expect(Object.keys(expectedHeaders('/admin/assets/x.js', status)).sort()).toEqual([
        'Cache-Control',
        'Content-Security-Policy',
        'Referrer-Policy',
        'X-Content-Type-Options',
        'X-Frame-Options',
      ])
    }
    expect(expectedHeaders('/api/v1/health', 200)).toEqual({})
  })

  it('adds frame-ancestors to the policy of the build, which a <meta> tag ignores', () => {
    const policy = SECURITY_HEADERS['Content-Security-Policy']
    expect(policy).toBe(`${CONTENT_SECURITY_POLICY}; frame-ancestors 'none'`)
    expect(policy.match(/frame-ancestors/g)).toHaveLength(1)
    expect(CONTENT_SECURITY_POLICY).not.toContain('frame-ancestors')
    expect(SECURITY_HEADERS['X-Frame-Options']).toBe('DENY')
  })
})

/**
 * A server that follows the contract, as a function that can stand in for `fetch`. A test breaks one
 * thing about it by answering one path differently.
 *
 * @param {Record<string, (path: string) => { status?: number, headers?: Record<string, string>, body?: string } | undefined>} [broken]
 * @returns {typeof fetch}
 */
function fakeServer(broken = {}) {
  const page = '<script type="module" src="/admin/assets/index-abc12345.js"></script><link rel="stylesheet" href="/admin/assets/index-def67890.css">'
  return /** @type {typeof fetch} */ (
    async (input, init) => {
      const path = new URL(String(input)).pathname
      const conditional = new Headers(init?.headers).has('if-none-match')
      let status = 404
      let body = '{"error":{"code":"NOT_FOUND"}}'
      /** @type {Record<string, string>} */
      let headers = {}
      if (path === '/admin') {
        status = 307
        body = ''
        headers = { location: 'http://localhost/admin/' }
      } else if (path === '/admin/' || path === '/admin/index.html') {
        status = 200
        body = page
      } else if (path.startsWith('/admin/assets/index-')) {
        status = conditional ? 304 : 200
        body = conditional ? '' : 'asset'
        headers = { etag: '"1"' }
      } else if (path === '/api/v1/health') {
        status = 200
        body = '{"status":"ok"}'
      }
      const own = broken[path]?.(path)
      status = own?.status ?? status
      body = own?.body ?? body
      // Header names are not case sensitive, so each one has to be set, not merged into an object.
      const merged = new Headers(headers)
      for (const [name, value] of Object.entries(expectedHeaders(path, status))) merged.set(name, value)
      for (const [name, value] of Object.entries(own?.headers ?? {})) merged.set(name, value)
      return new Response(status === 304 || status === 307 ? null : body, { status, headers: merged })
    }
  )
}

/**
 * @param {typeof fetch} fetch
 */
async function run(fetch) {
  return checkHosting('http://localhost:8000', { fetch })
}

/**
 * @param {Awaited<ReturnType<typeof run>>} checks
 */
const failures = (checks) => checks.filter((check) => !check.ok)

describe('check-hosting against a fake server', () => {
  it('passes a server that follows the contract', async () => {
    const checks = await run(fakeServer())
    expect(failures(checks)).toEqual([])
    expect(checks.map((check) => check.name)).toEqual([
      'GET /admin leads to /admin/',
      'GET /admin/',
      'GET /admin/index.html',
      'GET /admin/assets/index-abc12345.js',
      'GET /admin/assets/index-abc12345.js when the browser has it already',
      'GET /admin/assets/index-def67890.css',
      'GET /admin/assets/index-def67890.css when the browser has it already',
      'GET /admin/assets/missing-00000000.js, which does not exist',
      'GET /api/v1/health does not get the headers of the console',
    ])
    expect(report(checks)).toMatch(/All 9 checks passed\.$/)
  })

  it('does not mind the order of the directives of Cache-Control', async () => {
    const server = fakeServer({
      '/admin/assets/index-abc12345.js': () => ({ headers: { 'cache-control': 'immutable, max-age=31536000, public' } }),
    })
    expect(failures(await run(server))).toEqual([])
  })

  it.each([
    [
      'a page that the browser may keep without asking',
      { '/admin/': () => ({ headers: { 'cache-control': 'public, max-age=3600' } }) },
      ['GET /admin/'],
      'Cache-Control is "public, max-age=3600" (expected "no-cache")',
    ],
    [
      'a hashed file that is not cached for good',
      { '/admin/assets/index-abc12345.js': () => ({ headers: { 'cache-control': 'no-cache' } }) },
      // The conditional request for the same file gets the same wrong answer.
      ['GET /admin/assets/index-abc12345.js', 'GET /admin/assets/index-abc12345.js when the browser has it already'],
      `Cache-Control is "no-cache" (expected "${IMMUTABLE}")`,
    ],
    [
      'a file that does not exist but would be cached for a year',
      { '/admin/assets/missing-00000000.js': () => ({ headers: { 'cache-control': IMMUTABLE } }) },
      ['GET /admin/assets/missing-00000000.js, which does not exist'],
      `Cache-Control is "${IMMUTABLE}" (expected "no-cache")`,
    ],
    [
      'a policy without frame-ancestors',
      { '/admin/': () => ({ headers: { 'content-security-policy': CONTENT_SECURITY_POLICY } }) },
      ['GET /admin/'],
      `Content-Security-Policy is "${CONTENT_SECURITY_POLICY}" (expected the policy of csp.js plus frame-ancestors 'none')`,
    ],
    [
      'a console that can be framed by a page of another site',
      { '/admin/index.html': () => ({ headers: { 'x-frame-options': 'SAMEORIGIN' } }) },
      ['GET /admin/index.html'],
      'X-Frame-Options is "SAMEORIGIN" (expected "DENY")',
    ],
  ])('catches %s', async (_name, broken, names, problem) => {
    const bad = failures(await run(fakeServer(broken)))
    expect(bad.map((item) => item.name)).toEqual(names)
    for (const item of bad) expect(item.problems).toEqual([problem])
  })

  it('catches a response that lacks a header altogether', async () => {
    const server = /** @type {typeof fetch} */ (
      async (input, init) => {
        const response = await fakeServer()(input, init)
        const headers = new Headers(response.headers)
        if (new URL(String(input)).pathname === '/admin/') headers.delete('x-content-type-options')
        return new Response(response.body, { status: response.status, headers })
      }
    )
    const bad = failures(await run(server))
    expect(bad).toHaveLength(1)
    expect(bad[0].problems).toEqual(['X-Content-Type-Options is missing (expected "nosniff")'])
  })

  it('catches a redirect that leads somewhere else', async () => {
    const server = fakeServer({ '/admin': () => ({ headers: { location: '/somewhere-else/' } }) })
    const bad = failures(await run(server))
    expect(bad).toHaveLength(1)
    expect(bad[0].problems[0]).toMatch(/not a redirect to \/admin\//)
  })

  it('catches headers of the console leaking onto the API', async () => {
    const server = fakeServer({
      '/api/v1/health': () => ({
        headers: {
          'x-frame-options': SECURITY_HEADERS['X-Frame-Options'],
          'content-security-policy': SECURITY_HEADERS['Content-Security-Policy'],
          'cache-control': IMMUTABLE,
        },
      }),
    })
    const bad = failures(await run(server))
    expect(bad).toHaveLength(1)
    expect(bad[0].problems).toEqual([
      'X-Frame-Options of the console is also sent for /api/v1/health',
      'Content-Security-Policy of the console is also sent for /api/v1/health',
      '/api/v1/health is cached for a year',
    ])
  })

  it('leaves alone what the API sends for its own reasons', async () => {
    const server = fakeServer({
      '/api/v1/health': () => ({ headers: { 'x-content-type-options': 'nosniff', 'cache-control': 'no-store' } }),
    })
    expect(failures(await run(server))).toEqual([])
  })

  it('says when the page names no hashed file, which is a console that was never built', async () => {
    const server = fakeServer({ '/admin/': () => ({ body: '<html></html>' }) })
    const bad = failures(await run(server))
    expect(bad.map((item) => item.name)).toEqual(['the page names its hashed files'])
    expect(bad[0].problems[0]).toMatch(/is admin\/dist built\?/)
  })

  it('reports a server that cannot be reached, once for every request', async () => {
    const down = /** @type {typeof fetch} */ (
      async () => {
        throw new TypeError('fetch failed')
      }
    )
    const checks = await run(down)
    expect(checks.every((check) => !check.ok)).toBe(true)
    expect(checks[0].problems).toEqual(['the request failed: fetch failed'])
  })

  it('does not follow redirects, so that it sees the headers of the redirect itself', async () => {
    const seen = []
    const spy = /** @type {typeof fetch} */ (
      async (input, init) => {
        seen.push(init?.redirect)
        return fakeServer()(input, init)
      }
    )
    await run(spy)
    expect(new Set(seen)).toEqual(new Set(['manual']))
  })
})

describe('headerProblems', () => {
  it('has nothing to say about a response outside the console', () => {
    const response = new Response('{}', { status: 200 })
    expect(headerProblems(response, '/api/v1/health')).toEqual([])
  })
})

describe('check-hosting against a real server', () => {
  let work = ''

  beforeAll(() => {
    work = mkdtempSync(join(tmpdir(), 'alexandria-hosting-'))
    mkdirSync(join(work, 'assets'))
    writeFileSync(
      join(work, 'index.html'),
      '<!doctype html><script type="module" src="/admin/assets/index-abc12345.js"></script>',
    )
    writeFileSync(join(work, 'assets', 'index-abc12345.js'), 'console.log(1)')
  })

  afterAll(() => rmSync(work, { recursive: true, force: true }))

  it('passes the server that the browser tests use, which hosts the console like the registry has to', async () => {
    const site = await serveConsole({ root: work })
    try {
      const checks = await checkHosting(site.origin)
      expect(failures(checks)).toEqual([])
      expect(checks.length).toBeGreaterThanOrEqual(7)
    } finally {
      await site.close()
    }
  })

  it('fails a plain static server, and says what each response lacks', async () => {
    const site = await serveConsole({ root: work, hosting: false })
    try {
      const checks = await checkHosting(site.origin)
      const bad = failures(checks)
      expect(bad.map((item) => item.name)).not.toContain('GET /api/v1/health does not get the headers of the console')
      expect(bad.length).toBe(checks.length - 1)
      const text = report(checks)
      expect(text).toMatch(/FAIL {2}GET \/admin\/\n/)
      expect(text).toContain('X-Frame-Options is missing (expected "DENY")')
      expect(text).toContain('Cache-Control is missing (expected "no-cache")')
      expect(text).toContain(`Cache-Control is missing (expected "${IMMUTABLE}")`)
      expect(text).toMatch(/\d+ of \d+ checks failed\.$/)
    } finally {
      await site.close()
    }
  })
})
