import { CONTENT_SECURITY_POLICY } from './csp.js'

/**
 * What the console needs from whatever serves `dist/` at `/admin/`. The README ("Hosting") explains
 * why. `scripts/check-hosting.mjs` checks a running server against this, and the test server of the
 * browser tests behaves like this, so the console is only ever tested the way it has to be hosted.
 */

export const CONSOLE_PATH = '/admin'
export const ASSETS_PATH = '/admin/assets/'

/** Hashed files never change: one year, and no revalidation. */
export const IMMUTABLE = 'public, max-age=31536000, immutable'

/** The page names the hashed files of one build, and the next build deletes them. */
export const REVALIDATE = 'no-cache'

/** Sent with every response under `/admin`, including redirects and errors. */
export const SECURITY_HEADERS = Object.freeze({
  'X-Frame-Options': 'DENY',
  // A <meta> tag ignores frame-ancestors, which is why csp.js lacks it and the header has to add it.
  'Content-Security-Policy': `${CONTENT_SECURITY_POLICY}; frame-ancestors 'none'`,
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
})

/**
 * @param {string} path the path of the request, without the query
 * @returns {boolean}
 */
export function isConsolePath(path) {
  return path === CONSOLE_PATH || path.startsWith(`${CONSOLE_PATH}/`)
}

/**
 * Hashed assets are cached for good, but only when they exist: a cached 404 would pin a missing file
 * for a year. A 304 answers a revalidation of a file that does exist.
 *
 * @param {string} path
 * @param {number} status
 * @returns {string}
 */
export function expectedCacheControl(path, status) {
  const found = status === 200 || status === 304
  return path.startsWith(ASSETS_PATH) && found ? IMMUTABLE : REVALIDATE
}

/**
 * @param {string} path
 * @param {number} status
 * @returns {Record<string, string>} the headers the response must carry; none outside the console
 */
export function expectedHeaders(path, status) {
  if (!isConsolePath(path)) return {}
  return { ...SECURITY_HEADERS, 'Cache-Control': expectedCacheControl(path, status) }
}
