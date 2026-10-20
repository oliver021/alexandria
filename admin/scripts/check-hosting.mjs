#!/usr/bin/env node
// Checks that a running server hosts the console the way README.md ("Hosting") requires.
//
//   npm run check:hosting                      # http://localhost:8000
//   npm run check:hosting -- https://registry.example.test
//
// Exits with 0 when every check passes and with 1 when one fails. The requirements themselves live in
// ../hosting.js, so this script, the README and the test server of the browser tests cannot drift apart.

import { pathToFileURL } from 'node:url'
import { ASSETS_PATH, CONSOLE_PATH, SECURITY_HEADERS, expectedHeaders } from '../hosting.js'

const DEFAULT_BASE = 'http://localhost:8000'
const REQUEST_TIMEOUT_MS = 10_000
const REDIRECTS = new Set([301, 302, 307, 308])
const API_HEALTH = '/api/v1/health'

/**
 * @typedef {Object} Check
 * @property {string} name what was checked
 * @property {boolean} ok
 * @property {string[]} problems one sentence per difference, empty when ok
 */

/**
 * @param {string} value
 * @returns {string[]} the directives of a Cache-Control value, lower case and sorted
 */
function directivesOf(value) {
  return value
    .split(',')
    .map((part) => part.trim().toLowerCase())
    .filter(Boolean)
    .sort()
}

/**
 * Cache-Control directives have no order, so `immutable, public, max-age=1` is the same policy as
 * `public, max-age=1, immutable`.
 *
 * @param {string} actual
 * @param {string} expected
 * @returns {boolean}
 */
function samePolicy(actual, expected) {
  return directivesOf(actual).join(',') === directivesOf(expected).join(',')
}

/**
 * @param {string} name
 * @param {string} expected
 * @returns {string}
 */
function wanted(name, expected) {
  // The policy is long and the same in every message: say where it comes from instead.
  return name === 'Content-Security-Policy' ? "the policy of csp.js plus frame-ancestors 'none'" : `"${expected}"`
}

/**
 * @param {Response} response
 * @param {string} path the path that was requested
 * @returns {string[]} what differs from the headers that the console must get
 */
export function headerProblems(response, path) {
  const problems = []
  for (const [name, expected] of Object.entries(expectedHeaders(path, response.status))) {
    const actual = response.headers.get(name)
    if (actual === null) {
      problems.push(`${name} is missing (expected ${wanted(name, expected)})`)
    } else if (name === 'Cache-Control' ? !samePolicy(actual, expected) : actual !== expected) {
      problems.push(`${name} is "${actual}" (expected ${wanted(name, expected)})`)
    }
  }
  return problems
}

/**
 * @param {Response} response
 * @returns {string[]}
 */
function expectOk(response) {
  return response.status === 200 ? [] : [`the status is ${response.status}, expected 200`]
}

/**
 * @param {unknown} error
 * @returns {string[]}
 */
function failed(error) {
  return [`the request failed: ${error instanceof Error ? error.message : String(error)}`]
}

/**
 * @param {string} base where the registry listens, for example http://localhost:8000
 * @param {{ fetch?: typeof fetch }} [options]
 * @returns {Promise<Check[]>}
 */
export async function checkHosting(base, { fetch = globalThis.fetch } = {}) {
  const origin = new URL(base).origin
  /** @type {Check[]} */
  const checks = []

  /**
   * @param {string} path
   * @param {RequestInit} [init]
   */
  async function request(path, init = {}) {
    const response = await fetch(origin + path, {
      redirect: 'manual',
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      ...init,
    })
    return { response, body: await response.text() }
  }

  /**
   * A request whose response has to carry the headers of the console, plus whatever `more` finds.
   *
   * @param {string} name
   * @param {string} path
   * @param {(response: Response) => string[]} [more]
   * @param {RequestInit} [init]
   */
  async function check(name, path, more = () => [], init = {}) {
    try {
      const result = await request(path, init)
      const problems = [...headerProblems(result.response, path), ...more(result.response)]
      checks.push({ name, ok: problems.length === 0, problems })
      return result
    } catch (error) {
      checks.push({ name, ok: false, problems: failed(error) })
      return null
    }
  }

  await check(`GET ${CONSOLE_PATH} leads to ${CONSOLE_PATH}/`, CONSOLE_PATH, (response) => {
    if (response.status === 200) return []
    const location = response.headers.get('location') ?? ''
    const target = location ? new URL(location, origin).pathname : ''
    if (REDIRECTS.has(response.status) && target === `${CONSOLE_PATH}/`) return []
    return [`it answers ${response.status} with Location "${location}", not a redirect to ${CONSOLE_PATH}/`]
  })

  const page = await check(`GET ${CONSOLE_PATH}/`, `${CONSOLE_PATH}/`, expectOk)
  await check(`GET ${CONSOLE_PATH}/index.html`, `${CONSOLE_PATH}/index.html`, expectOk)

  const assets = [...(page?.body ?? '').matchAll(/(?:src|href)="(\/admin\/assets\/[^"?#]+)"/g)].map(
    (match) => match[1],
  )
  if (assets.length === 0) {
    checks.push({
      name: 'the page names its hashed files',
      ok: false,
      problems: [`${CONSOLE_PATH}/ references nothing under ${ASSETS_PATH}; is admin/dist built?`],
    })
  }
  for (const asset of assets) {
    const found = await check(`GET ${asset}`, asset, expectOk)
    const validator = found?.response.headers.get('etag')
    if (validator) {
      await check(
        `GET ${asset} when the browser has it already`,
        asset,
        (response) => (response.status === 304 ? [] : [`the status is ${response.status}, expected 304`]),
        { headers: { 'If-None-Match': validator } },
      )
    }
  }

  // A file that does not exist must never be cached for a year.
  const missing = `${ASSETS_PATH}missing-00000000.js`
  await check(`GET ${missing}, which does not exist`, missing, (response) =>
    response.status === 404 ? [] : [`the status is ${response.status}, expected 404`],
  )

  // The headers belong to the console. Those that only make sense for a page (framing, a policy for
  // scripts, a year of cache) must not leak onto the API.
  const apiName = `GET ${API_HEALTH} does not get the headers of the console`
  try {
    const { response } = await request(API_HEALTH)
    const problems = ['X-Frame-Options', 'Content-Security-Policy']
      .filter((name) => response.headers.get(name) === SECURITY_HEADERS[name])
      .map((name) => `${name} of the console is also sent for ${API_HEALTH}`)
    if ((response.headers.get('cache-control') ?? '').toLowerCase().includes('immutable')) {
      problems.push(`${API_HEALTH} is cached for a year`)
    }
    checks.push({ name: apiName, ok: problems.length === 0, problems })
  } catch (error) {
    checks.push({ name: apiName, ok: false, problems: failed(error) })
  }

  return checks
}

/**
 * @param {Check[]} checks
 * @returns {string}
 */
export function report(checks) {
  const lines = []
  for (const { name, ok, problems } of checks) {
    lines.push(`${ok ? 'ok  ' : 'FAIL'}  ${name}`)
    for (const problem of problems) lines.push(`        ${problem}`)
  }
  const bad = checks.filter((item) => !item.ok).length
  lines.push('', bad === 0 ? `All ${checks.length} checks passed.` : `${bad} of ${checks.length} checks failed.`)
  return lines.join('\n')
}

async function main() {
  const base = process.argv[2] ?? process.env.ALEXANDRIA_URL ?? DEFAULT_BASE
  console.log(`Checking how ${base} hosts the console at ${CONSOLE_PATH}/\n`)
  const checks = await checkHosting(base)
  console.log(report(checks))
  process.exitCode = checks.every((item) => item.ok) ? 0 : 1
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await main()
}
