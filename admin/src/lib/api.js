import { session } from './auth.svelte.js'
import { encodeFilePath, encodeSegment } from './paths.js'
import { router } from './router.svelte.js'
import { loginQuery, paths, queryString } from './routes.js'

export const API_BASE = '/api/v1'

const NETWORK_MESSAGE = 'The registry could not be reached. Check your connection and try again.'
const SESSION_EXPIRED_MESSAGE = 'Your session has expired. Sign in again.'

export class ApiError extends Error {
  /**
   * @param {{ status: number, code: string, message: string, reason?: string | null }} details
   */
  constructor({ status, code, message, reason = null }) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.reason = reason
  }
}

/**
 * @param {unknown} error
 * @returns {error is ApiError}
 */
export function isApiError(error) {
  return error instanceof ApiError
}

/**
 * @param {unknown} error
 * @returns {boolean}
 */
export function isAbortError(error) {
  return Boolean(error) && typeof error === 'object' && /** @type {any} */ (error).name === 'AbortError'
}

/**
 * @param {unknown} error
 * @returns {boolean}
 */
export function isUnauthorized(error) {
  return isApiError(error) && error.status === 401
}

/**
 * @param {unknown} error
 * @returns {boolean}
 */
export function isMissingEndpoint(error) {
  return (
    isApiError(error) &&
    (error.status === 405 ||
      error.status === 501 ||
      (error.status === 404 && error.message === 'Not Found'))
  )
}

/**
 * @param {number} status
 * @param {string} statusText
 * @returns {string}
 */
function fallbackMessage(status, statusText) {
  return `The request failed (${status}${statusText ? ` ${statusText}` : ''}).`
}

/**
 * @param {number} status
 * @param {string} text
 * @param {string} [statusText]
 * @returns {ApiError}
 */
export function errorFromParts(status, text, statusText = '') {
  /** @type {any} */
  let parsed = null
  try {
    parsed = text ? JSON.parse(text) : null
  } catch {
    parsed = null
  }
  const detail = parsed && typeof parsed === 'object' ? parsed.error : null
  if (detail && typeof detail === 'object' && typeof detail.code === 'string') {
    return new ApiError({
      status,
      code: detail.code,
      message:
        typeof detail.message === 'string' && detail.message
          ? detail.message
          : fallbackMessage(status, statusText),
      reason: typeof detail.reason === 'string' ? detail.reason : null,
    })
  }
  return new ApiError({
    status,
    code: `HTTP_${status}`,
    message: fallbackMessage(status, statusText),
    reason: null,
  })
}

/**
 * @param {string} sentToken
 */
function expireSession(sentToken) {
  if (session.token !== sentToken) return
  session.end(SESSION_EXPIRED_MESSAGE)
  if (router.route.name !== 'login') {
    router.navigate(paths.login(), { query: loginQuery(router.route.hash) })
  }
}

/**
 * @param {Response} response
 * @returns {Promise<ApiError>}
 */
async function errorFromResponse(response) {
  let text = ''
  try {
    text = await response.text()
  } catch {
    text = ''
  }
  return errorFromParts(response.status, text, response.statusText)
}

/**
 * @param {Response} response
 * @returns {Promise<any>}
 */
async function readJson(response) {
  const text = await response.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    throw new ApiError({
      status: response.status,
      code: 'INVALID_RESPONSE',
      message: 'The registry returned a response that is not valid JSON.',
    })
  }
}

/**
 * @typedef {Object} RequestOptions
 * @property {string} [method]
 * @property {import('./routes.js').QueryInput} [query]
 * @property {unknown} [json]
 * @property {BodyInit | null} [body]
 * @property {AbortSignal} [signal]
 * @property {boolean} [auth]
 * @property {'json' | 'blob' | 'none'} [expect]
 * @property {boolean} [redirectOnUnauthorized]
 */

/**
 * @param {string} path
 * @param {RequestOptions} [options]
 * @returns {Promise<any>}
 */
async function request(path, options = {}) {
  const {
    method = 'GET',
    query,
    json,
    body = null,
    signal,
    auth = true,
    expect = 'json',
    redirectOnUnauthorized = true,
  } = options
  const token = auth ? session.token : null
  /** @type {Record<string, string>} */
  const headers = { Accept: expect === 'json' ? 'application/json' : '*/*' }
  let payload = body
  if (json !== undefined) {
    headers['Content-Type'] = 'application/json'
    payload = JSON.stringify(json)
  }
  if (token) headers.Authorization = `Bearer ${token}`

  let response
  try {
    response = await fetch(`${API_BASE}${path}${queryString(query)}`, {
      method,
      headers,
      body: payload,
      signal,
      cache: 'no-store',
    })
  } catch (cause) {
    if (isAbortError(cause)) throw cause
    throw new ApiError({ status: 0, code: 'NETWORK_ERROR', message: NETWORK_MESSAGE })
  }

  if (!response.ok) {
    const error = await errorFromResponse(response)
    if (response.status === 401 && token && redirectOnUnauthorized) expireSession(token)
    throw error
  }
  if (response.status === 204 || expect === 'none') return null
  if (expect === 'blob') {
    return { blob: await response.blob(), headers: response.headers }
  }
  return readJson(response)
}

/**
 * @param {string} ref
 * @returns {string}
 */
function entryPath(ref) {
  return `/skills/${encodeSegment(ref)}`
}

/**
 * @param {string} ref
 * @param {string} version
 * @returns {string}
 */
function versionPath(ref, version) {
  return `${entryPath(ref)}/versions/${encodeSegment(version)}`
}

/**
 * @typedef {Object} UploadOptions
 * @property {string} [version]
 * @property {boolean} [publish]
 * @property {(progress: { loaded: number, total: number, done: boolean }) => void} [onProgress]
 * @property {AbortSignal} [signal]
 */

/**
 * @param {File} file
 * @param {UploadOptions} [options]
 * @returns {Promise<import('./types.js').UploadResult>}
 */
function uploadPackage(file, { version, publish, onProgress, signal } = {}) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('The upload was cancelled', 'AbortError'))
      return
    }
    const token = session.token
    const xhr = new XMLHttpRequest()
    const form = new FormData()
    form.append('file', file, file.name)
    if (version) form.append('version', version)
    if (typeof publish === 'boolean') form.append('publish', publish ? 'true' : 'false')

    xhr.open('POST', `${API_BASE}/packages`)
    xhr.responseType = 'text'
    xhr.setRequestHeader('Accept', 'application/json')
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)

    const total = file.size
    xhr.upload.onprogress = (event) => {
      onProgress?.({
        loaded: event.loaded,
        total: event.lengthComputable ? event.total : total,
        done: false,
      })
    }
    xhr.upload.onload = () => {
      onProgress?.({ loaded: total, total, done: true })
    }
    xhr.onerror = () => {
      reject(new ApiError({ status: 0, code: 'NETWORK_ERROR', message: NETWORK_MESSAGE }))
    }
    xhr.ontimeout = xhr.onerror
    xhr.onabort = () => {
      reject(new DOMException('The upload was cancelled', 'AbortError'))
    }
    xhr.onload = () => {
      const text = typeof xhr.responseText === 'string' ? xhr.responseText : ''
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          const data = JSON.parse(text)
          resolve({ entry: data.skill, version: data.version })
        } catch {
          reject(
            new ApiError({
              status: xhr.status,
              code: 'INVALID_RESPONSE',
              message: 'The registry returned a response that is not valid JSON.',
            }),
          )
        }
        return
      }
      const error = errorFromParts(xhr.status, text, xhr.statusText)
      if (xhr.status === 401 && token) expireSession(token)
      reject(error)
    }

    signal?.addEventListener('abort', () => xhr.abort(), { once: true })
    xhr.send(form)
  })
}

const OPENAPI_URL = '/openapi.json'
const DEACTIVATION_ROUTE = /^\/api\/v1\/users\/\{[^}/]+\}(?:\/[^/]+)?$/
const DEACTIVATION_METHODS = ['post', 'put', 'patch', 'delete']

/**
 * @typedef {{ method: string, path: string }} DiscoveredRoute
 * @typedef {'available' | 'missing' | 'unknown'} DeactivationSupport
 */

/** @type {Promise<DiscoveredRoute | null | undefined> | null} */
let deactivationDiscovery = null

export function resetRouteDiscovery() {
  deactivationDiscovery = null
}

/**
 * @returns {Promise<DiscoveredRoute | null | undefined>}
 */
async function discoverDeactivation() {
  try {
    const response = await fetch(OPENAPI_URL, { headers: { Accept: 'application/json' }, cache: 'no-store' })
    if (!response.ok) return undefined
    const spec = await response.json()
    if (!spec || typeof spec !== 'object' || typeof spec.paths !== 'object' || spec.paths === null) {
      return undefined
    }
    for (const [route, operations] of Object.entries(spec.paths)) {
      if (!DEACTIVATION_ROUTE.test(route) || !operations || typeof operations !== 'object') continue
      for (const [method, operation] of Object.entries(operations)) {
        if (!DEACTIVATION_METHODS.includes(method)) continue
        const details = /** @type {any} */ (operation)
        const text = [route, details?.operationId, details?.summary].join(' ').toLowerCase()
        if (text.includes('deactivat')) {
          return { method: method.toUpperCase(), path: route.replace(/^\/api\/v1/, '') }
        }
      }
    }
    return null
  } catch {
    return undefined
  }
}

/**
 * @returns {Promise<DiscoveredRoute | null | undefined>}
 */
function findDeactivation() {
  if (!deactivationDiscovery) {
    const attempt = discoverDeactivation()
    deactivationDiscovery = attempt
    attempt.then((route) => {
      if (route === undefined && deactivationDiscovery === attempt) deactivationDiscovery = null
    })
  }
  return deactivationDiscovery
}

/**
 * @returns {Promise<DeactivationSupport>}
 */
async function checkUserDeactivation() {
  const route = await findDeactivation()
  if (route) return 'available'
  return route === null ? 'missing' : 'unknown'
}

/**
 * @param {string} id
 * @returns {Promise<any>}
 */
async function deactivateUser(id) {
  const segment = encodeSegment(id)
  const route = await findDeactivation()
  if (!route) {
    throw new ApiError({ status: 404, code: 'NOT_FOUND', message: 'Not Found' })
  }
  return request(route.path.replace(/\{[^}]+\}/, segment), { method: route.method })
}

/**
 * @typedef {{ signal?: AbortSignal }} CallOptions
 * @typedef {Object} ListParams
 * @property {string} [q]
 * @property {string} [kind]
 * @property {string[]} [tag]
 * @property {string} [category]
 * @property {string} [status]
 * @property {boolean} [mine]
 * @property {string} [sort]
 * @property {number} [limit]
 * @property {number} [offset]
 */

export const api = {
  /**
   * @param {CallOptions} [options]
   * @returns {Promise<import('./types.js').RegistryInfo>}
   */
  info: (options = {}) => request('/info', { ...options, auth: false }),

  /**
   * @param {string} username
   * @param {string} password
   * @returns {Promise<{ token: string, user: import('./types.js').User }>}
   */
  login: (username, password) =>
    request('/auth/login', {
      method: 'POST',
      json: { username, password },
      auth: false,
      redirectOnUnauthorized: false,
    }),

  /**
   * @param {CallOptions} [options]
   * @returns {Promise<import('./types.js').User>}
   */
  me: (options = {}) => request('/auth/me', options),

  /**
   * @param {ListParams} [params]
   * @param {CallOptions} [options]
   * @returns {Promise<import('./types.js').Page>}
   */
  listEntries: (params = {}, options = {}) => request('/skills', { ...options, query: params }),

  /**
   * @param {string} ref
   * @param {CallOptions} [options]
   * @returns {Promise<import('./types.js').EntryDetail>}
   */
  getEntry: (ref, options = {}) => request(entryPath(ref), options),

  /**
   * @param {string} ref
   * @returns {Promise<null>}
   */
  hideEntry: (ref) => request(entryPath(ref), { method: 'DELETE' }),

  /**
   * @param {string} ref
   * @returns {Promise<import('./types.js').EntryDetail>}
   */
  restoreEntry: (ref) => request(`${entryPath(ref)}/restore`, { method: 'POST' }),

  /**
   * @param {string} ref
   * @param {string} version
   * @param {CallOptions} [options]
   * @returns {Promise<import('./types.js').VersionDetail>}
   */
  getVersion: (ref, version, options = {}) => request(versionPath(ref, version), options),

  /**
   * @param {string} ref
   * @param {string} version
   * @returns {Promise<import('./types.js').VersionSummary>}
   */
  publishVersion: (ref, version) =>
    request(`${versionPath(ref, version)}/publish`, { method: 'POST' }),

  /**
   * @param {string} ref
   * @param {string} version
   * @returns {Promise<import('./types.js').VersionSummary>}
   */
  deprecateVersion: (ref, version) =>
    request(`${versionPath(ref, version)}/deprecate`, { method: 'POST' }),

  /**
   * @param {string} ref
   * @param {string} version
   * @param {CallOptions} [options]
   * @returns {Promise<import('./types.js').FileListing>}
   */
  listFiles: (ref, version, options = {}) =>
    request(`${versionPath(ref, version)}/files`, options),

  /**
   * @param {string} ref
   * @param {string} version
   * @param {string} path
   * @param {CallOptions} [options]
   * @returns {Promise<import('./types.js').FileContent>}
   */
  readFile: (ref, version, path, options = {}) =>
    request(`${versionPath(ref, version)}/files/${encodeFilePath(path)}`, options),

  /**
   * @param {string} ref
   * @param {string} version
   * @param {string} path
   * @param {CallOptions} [options]
   * @returns {Promise<Blob>}
   */
  downloadFile: async (ref, version, path, options = {}) => {
    const { blob } = await request(`${versionPath(ref, version)}/raw/${encodeFilePath(path)}`, {
      ...options,
      expect: 'blob',
    })
    return blob
  },

  /**
   * @param {string} ref
   * @param {string} version
   * @param {CallOptions} [options]
   * @returns {Promise<{ blob: Blob, sha256: string | null }>}
   */
  downloadPackage: async (ref, version, options = {}) => {
    const { blob, headers } = await request(`${versionPath(ref, version)}/download`, {
      ...options,
      expect: 'blob',
    })
    return { blob, sha256: headers.get('X-Content-SHA256') }
  },

  uploadPackage,

  /**
   * @param {{ kind?: string, limit?: number }} [params]
   * @param {CallOptions} [options]
   * @returns {Promise<import('./types.js').TaxonomyItem[]>}
   */
  listTags: (params = {}, options = {}) => request('/tags', { ...options, query: params }),

  /**
   * @param {{ kind?: string, limit?: number }} [params]
   * @param {CallOptions} [options]
   * @returns {Promise<import('./types.js').TaxonomyItem[]>}
   */
  listCategories: (params = {}, options = {}) =>
    request('/categories', { ...options, query: params }),

  /**
   * @param {CallOptions} [options]
   * @returns {Promise<import('./types.js').ApiToken[]>}
   */
  listTokens: (options = {}) => request('/tokens', options),

  /**
   * @param {{ name: string, scope: string, expires_in_days?: number }} data
   * @returns {Promise<import('./types.js').CreatedToken>}
   */
  createToken: (data) => request('/tokens', { method: 'POST', json: data }),

  /**
   * @param {string} id
   * @returns {Promise<null>}
   */
  revokeToken: (id) => request(`/tokens/${encodeSegment(id)}`, { method: 'DELETE' }),

  /**
   * @param {CallOptions} [options]
   * @returns {Promise<import('./types.js').User[]>}
   */
  listUsers: (options = {}) => request('/users', options),

  /**
   * @param {{ username: string, email: string, password: string, role: string }} data
   * @returns {Promise<import('./types.js').User>}
   */
  createUser: (data) => request('/users', { method: 'POST', json: data }),

  checkUserDeactivation,

  deactivateUser,
}
