import { encodeSegment } from './paths.js'

/**
 * @typedef {Object} RouteDefinition
 * @property {string} name
 * @property {string} pattern
 * @property {string} title
 * @property {boolean} [admin]
 * @property {boolean} [anonymous]
 */

/** @type {readonly RouteDefinition[]} */
export const ROUTES = Object.freeze([
  { name: 'dashboard', pattern: '/', title: 'Dashboard' },
  { name: 'dashboard', pattern: '/dashboard', title: 'Dashboard' },
  { name: 'library', pattern: '/library', title: 'Library' },
  { name: 'entry', pattern: '/library/:name', title: 'Entry' },
  { name: 'upload', pattern: '/upload', title: 'Upload package' },
  { name: 'tokens', pattern: '/tokens', title: 'API tokens' },
  { name: 'users', pattern: '/users', title: 'Users', admin: true },
  { name: 'login', pattern: '/login', title: 'Sign in', anonymous: true },
])

export const paths = Object.freeze({
  dashboard: () => '/',
  library: () => '/library',
  /** @param {string} name */
  entry: (name) => `/library/${encodeSegment(name)}`,
  upload: () => '/upload',
  tokens: () => '/tokens',
  users: () => '/users',
  login: () => '/login',
})

/**
 * @typedef {URLSearchParams | Record<string, unknown> | Array<[string, unknown]>} QueryInput
 */

/**
 * @param {QueryInput | null | undefined} query
 * @returns {string}
 */
export function queryString(query) {
  if (!query) return ''
  const params = new URLSearchParams()
  /**
   * @param {string} key
   * @param {unknown} value
   */
  const append = (key, value) => {
    if (value === undefined || value === null || value === '' || value === false) return
    params.append(key, value === true ? '1' : String(value))
  }
  if (query instanceof URLSearchParams) {
    query.forEach((value, key) => append(key, value))
  } else if (Array.isArray(query)) {
    for (const [key, value] of query) append(key, value)
  } else {
    for (const [key, value] of Object.entries(query)) {
      if (Array.isArray(value)) value.forEach((item) => append(key, item))
      else append(key, value)
    }
  }
  const text = params.toString()
  return text ? `?${text}` : ''
}

/**
 * @param {string} path
 * @param {QueryInput | null} [query]
 * @returns {string}
 */
export function hrefFor(path, query) {
  return `#${path}${queryString(query)}`
}

/**
 * @param {string} [hash]
 * @returns {{ path: string, query: URLSearchParams }}
 */
export function parseHash(hash = '') {
  const raw = hash.startsWith('#') ? hash.slice(1) : hash
  const split = raw.indexOf('?')
  const pathPart = split === -1 ? raw : raw.slice(0, split)
  const queryPart = split === -1 ? '' : raw.slice(split + 1)
  let path = pathPart.startsWith('/') ? pathPart : `/${pathPart}`
  path = path.length > 1 ? path.replace(/\/+$/, '') || '/' : path
  return { path, query: new URLSearchParams(queryPart) }
}

/**
 * @param {string} pattern
 * @param {string} path
 * @returns {Record<string, string> | null}
 */
export function matchPattern(pattern, path) {
  const patternSegments = pattern.split('/').filter(Boolean)
  const pathSegments = path.split('/').filter(Boolean)
  if (patternSegments.length !== pathSegments.length) return null
  /** @type {Record<string, string>} */
  const params = {}
  for (let index = 0; index < patternSegments.length; index += 1) {
    const expected = patternSegments[index]
    const actual = pathSegments[index]
    if (expected.startsWith(':')) {
      try {
        params[expected.slice(1)] = decodeURIComponent(actual)
      } catch {
        return null
      }
    } else if (expected !== actual) {
      return null
    }
  }
  return params
}

/**
 * @typedef {Object} ResolvedRoute
 * @property {string} name
 * @property {string} title
 * @property {Record<string, string>} params
 * @property {string} path
 * @property {URLSearchParams} query
 * @property {string} hash
 * @property {boolean} admin
 * @property {boolean} anonymous
 */

/**
 * @param {string} hash
 * @returns {ResolvedRoute}
 */
export function resolveHash(hash) {
  const { path, query } = parseHash(hash)
  const normalized = `${path}${queryString(query)}`
  for (const route of ROUTES) {
    const params = matchPattern(route.pattern, path)
    if (params) {
      return {
        name: route.name,
        title: route.title,
        params,
        path,
        query,
        hash: normalized,
        admin: Boolean(route.admin),
        anonymous: Boolean(route.anonymous),
      }
    }
  }
  return {
    name: 'not-found',
    title: 'Page not found',
    params: {},
    path,
    query,
    hash: normalized,
    admin: false,
    anonymous: false,
  }
}

/**
 * @param {string} name
 * @param {QueryInput | null} [query]
 * @returns {string}
 */
export function entryHref(name, query) {
  try {
    return hrefFor(paths.entry(name), query)
  } catch {
    return hrefFor(paths.library(), { q: String(name) })
  }
}

/**
 * @param {string} hash
 * @returns {{ next: string } | null}
 */
export function loginQuery(hash) {
  const resolved = resolveHash(`#${hash}`)
  if (resolved.name === 'not-found' || resolved.name === 'login' || resolved.name === 'dashboard') {
    return null
  }
  return { next: resolved.hash }
}

/**
 * @param {string | null | undefined} value
 * @returns {string}
 */
export function safeNext(value) {
  if (typeof value !== 'string' || !value.startsWith('/') || value.startsWith('//')) return '/'
  if (/[\\\u0000-\u001f]/.test(value)) return '/'
  const resolved = resolveHash(`#${value}`)
  if (resolved.name === 'not-found' || resolved.name === 'login') return '/'
  return resolved.hash
}
