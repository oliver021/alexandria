import { vi } from 'vitest'
import { resetRouteDiscovery } from '../lib/api.js'
import { session } from '../lib/auth.svelte.js'
import { toasts } from '../lib/toast.svelte.js'

export const ADMIN = Object.freeze({
  id: 'user-admin-0001',
  username: 'ada',
  email: 'ada@example.test',
  role: 'admin',
  is_active: true,
  created_at: '2026-01-01T10:00:00Z',
})

export const PUBLISHER = Object.freeze({
  id: 'user-publisher-0002',
  username: 'grace',
  email: 'grace@example.test',
  role: 'publisher',
  is_active: true,
  created_at: '2026-01-02T10:00:00Z',
})

export function signIn(user = ADMIN, token = 'jwt-session-token') {
  session.start(token, { ...user })
}

/**
 * The elements that the page scrolled into view since the last `resetApp()`, with the options used.
 * jsdom does not scroll, so this is how a test sees that an error was brought into the window.
 *
 * @returns {{ element: Element, options: ScrollIntoViewOptions }[]}
 */
export function scrolledIntoView() {
  const spy = /** @type {import('vitest').Mock} */ (Element.prototype.scrollIntoView)
  return spy.mock.contexts.map((element, index) => ({
    element: /** @type {Element} */ (element),
    options: spy.mock.calls[index][0],
  }))
}

export function resetApp() {
  vi.mocked(Element.prototype.scrollIntoView).mockClear()
  resetRouteDiscovery()
  session.end()
  window.sessionStorage.clear()
  window.localStorage.clear()
  toasts.clear()
  window.history.replaceState(null, '', '#/')
  vi.unstubAllGlobals()
  vi.useRealTimers()
}

export function jsonResponse(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  })
}

export function errorResponse(status, code, message, reason) {
  const error = { code, message }
  if (reason) error.reason = reason
  return jsonResponse({ error }, status)
}

export function emptyResponse(status = 204) {
  return new Response(null, { status })
}

export function binaryResponse(bytes, headers = {}) {
  return new Response(new Uint8Array(bytes), {
    status: 200,
    headers: { 'content-type': 'application/octet-stream', ...headers },
  })
}

function matches(handler, call) {
  if (handler.method !== call.method) return false
  if (typeof handler.path === 'string') return handler.path === call.path
  return handler.path.test(call.path)
}

export function mockFetch(handlers = []) {
  const calls = []
  const table = [...handlers]
  const fetchMock = vi.fn(async (input, init = {}) => {
    const url = new URL(String(input), 'http://localhost')
    const call = {
      method: (init.method ?? 'GET').toUpperCase(),
      url,
      path: url.pathname.replace(/^\/api\/v1/, ''),
      query: url.searchParams,
      headers: init.headers ?? {},
      body: init.body,
      signal: init.signal,
    }
    calls.push(call)
    const handler = table.find((candidate) => matches(candidate, call))
    if (!handler) throw new Error(`Unhandled request: ${call.method} ${call.path}`)
    const result =
      typeof handler.respond === 'function' ? await handler.respond(call) : handler.respond
    return result instanceof Response ? result.clone() : result
  })
  vi.stubGlobal('fetch', fetchMock)
  return {
    fetch: fetchMock,
    calls,
    on(method, path, respond) {
      table.unshift({ method, path, respond })
    },
    callsTo(method, path) {
      return calls.filter((call) => call.method === method && matches({ method, path }, call))
    },
  }
}

export class FakeXHR {
  static instances = []

  constructor() {
    this.upload = { onprogress: null, onload: null }
    this.headers = {}
    this.status = 0
    this.statusText = ''
    this.responseText = ''
    this.responseType = ''
    this.sent = null
    this.aborted = false
    this.onload = null
    this.onerror = null
    this.onabort = null
    this.ontimeout = null
    FakeXHR.instances.push(this)
  }

  open(method, url) {
    this.method = method
    this.url = url
  }

  setRequestHeader(name, value) {
    this.headers[name] = value
  }

  send(body) {
    this.sent = body
  }

  abort() {
    this.aborted = true
    this.onabort?.()
  }

  progress(loaded, total) {
    this.upload.onprogress?.({ loaded, total, lengthComputable: true })
  }

  uploaded() {
    this.upload.onload?.()
  }

  respond(status, body, statusText = '') {
    this.status = status
    this.statusText = statusText
    this.responseText = typeof body === 'string' ? body : JSON.stringify(body)
    this.onload?.()
  }

  fail() {
    this.onerror?.()
  }
}

export function installFakeXHR() {
  FakeXHR.instances = []
  vi.stubGlobal('XMLHttpRequest', FakeXHR)
  return FakeXHR
}

export function entrySummary(overrides = {}) {
  return {
    id: 'entry-1',
    name: 'pdf-forms',
    display_name: 'PDF Forms',
    description: 'Fill and read PDF forms.',
    kind: 'skill',
    category: 'documents',
    tags: ['pdf', 'forms'],
    status: 'published',
    latest_version: '1.2.0',
    owner_id: ADMIN.id,
    created_at: '2026-02-01T10:00:00Z',
    updated_at: '2026-03-01T10:00:00Z',
    ...overrides,
  }
}

export function versionSummary(overrides = {}) {
  return {
    id: `version-${overrides.version ?? '1.2.0'}`,
    skill_id: 'entry-1',
    version: '1.2.0',
    status: 'published',
    sha256: 'a'.repeat(64),
    size_bytes: 2048,
    uncompressed_bytes: 4096,
    file_count: 3,
    entrypoint: 'SKILL.md',
    display_name: 'PDF Forms',
    description: 'Fill and read PDF forms.',
    category: 'documents',
    tags: ['pdf', 'forms'],
    uploaded_by: ADMIN.id,
    published_by: ADMIN.id,
    published_at: '2026-03-01T10:00:00Z',
    deprecated_at: null,
    created_at: '2026-03-01T09:00:00Z',
    ...overrides,
  }
}

export function entryDetail(overrides = {}, versions = [versionSummary()]) {
  return { ...entrySummary(overrides), versions }
}

export function registryInfo(overrides = {}) {
  return {
    name: 'Alexandria',
    version: '0.1.0',
    public_read: true,
    public_url: 'https://registry.example.test',
    mcp_path: '/mcp',
    limits: {
      max_package_bytes: 26214400,
      max_uncompressed_bytes: 104857600,
      max_file_bytes: 52428800,
      max_files: 2000,
      max_inline_read_bytes: 262144,
    },
    ...overrides,
  }
}
