import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ADMIN,
  FakeXHR,
  binaryResponse,
  emptyResponse,
  errorResponse,
  installFakeXHR,
  jsonResponse,
  mockFetch,
  resetApp,
  signIn,
} from '../test/helpers.js'
import {
  ApiError,
  api,
  errorFromParts,
  isAbortError,
  isApiError,
  isMissingEndpoint,
  resetRouteDiscovery,
} from './api.js'
import { session } from './auth.svelte.js'
import { router } from './router.svelte.js'

const TOKEN = 'jwt-secret-token-value'

beforeEach(() => {
  resetApp()
  router.stop()
  router.start()
})

afterEach(() => {
  router.stop()
  resetApp()
})

function headersOf(call) {
  return Object.fromEntries(Object.entries(call.headers).map(([k, v]) => [k.toLowerCase(), v]))
}

describe('request headers', () => {
  it('sends the bearer token and asks for JSON when signed in', async () => {
    signIn(ADMIN, TOKEN)
    const http = mockFetch([{ method: 'GET', path: '/skills', respond: () => jsonResponse({}) }])
    await api.listEntries()
    const headers = headersOf(http.calls[0])
    expect(headers.authorization).toBe(`Bearer ${TOKEN}`)
    expect(headers.accept).toBe('application/json')
    expect(headers['content-type']).toBeUndefined()
  })

  it('sends no credential when signed out', async () => {
    const http = mockFetch([{ method: 'GET', path: '/skills', respond: () => jsonResponse({}) }])
    await api.listEntries()
    expect(headersOf(http.calls[0]).authorization).toBeUndefined()
  })

  it('does not send the session to public endpoints', async () => {
    signIn(ADMIN, TOKEN)
    const http = mockFetch([{ method: 'GET', path: '/info', respond: () => jsonResponse({}) }])
    await api.info()
    expect(headersOf(http.calls[0]).authorization).toBeUndefined()
  })

  it('declares JSON only when it sends a JSON body', async () => {
    signIn(ADMIN, TOKEN)
    const http = mockFetch([
      { method: 'POST', path: '/tokens', respond: () => jsonResponse({ token: 'alx_x' }, 201) },
    ])
    await api.createToken({ name: 'ci', scope: 'read' })
    const call = http.calls[0]
    expect(headersOf(call)['content-type']).toBe('application/json')
    expect(JSON.parse(call.body)).toEqual({ name: 'ci', scope: 'read' })
  })

  it('never puts a credential in the URL', async () => {
    signIn(ADMIN, TOKEN)
    const http = mockFetch([
      { method: 'GET', path: /.*/, respond: () => jsonResponse({}) },
      { method: 'POST', path: /.*/, respond: () => jsonResponse({}) },
      { method: 'DELETE', path: /.*/, respond: () => emptyResponse() },
    ])
    await api.listEntries({ q: 'x' })
    await api.getEntry('pdf-forms')
    await api.listFiles('pdf-forms', '1.0.0')
    await api.readFile('pdf-forms', '1.0.0', 'a/b.md')
    await api.publishVersion('pdf-forms', '1.0.0')
    await api.hideEntry('pdf-forms')
    await api.listTokens()
    for (const call of http.calls) {
      expect(call.url.href).not.toContain(TOKEN)
      expect(call.url.search).not.toMatch(/token|auth|bearer/i)
    }
  })
})

describe('URLs', () => {
  it('serializes query parameters, repeating tags and skipping empty values', async () => {
    const http = mockFetch([{ method: 'GET', path: '/skills', respond: () => jsonResponse({}) }])
    await api.listEntries({
      q: 'pdf forms',
      kind: 'skill',
      tag: ['a', 'b'],
      category: '',
      status: undefined,
      mine: true,
      limit: 20,
      offset: 40,
    })
    const query = http.calls[0].query
    expect(query.get('q')).toBe('pdf forms')
    expect(query.getAll('tag')).toEqual(['a', 'b'])
    expect(query.get('mine')).toBe('1')
    expect(query.get('limit')).toBe('20')
    expect(query.get('offset')).toBe('40')
    expect(query.has('category')).toBe(false)
    expect(query.has('status')).toBe(false)
  })

  it('encodes route parameters', async () => {
    const http = mockFetch([{ method: 'GET', path: /.*/, respond: () => jsonResponse({}) }])
    await api.getVersion('pdf forms', '1.0.0-rc.1+x')
    await api.readFile('pdf-forms', '1.0.0', 'refs/api notes/ñ.md')
    expect(http.calls[0].url.pathname).toBe('/api/v1/skills/pdf%20forms/versions/1.0.0-rc.1%2Bx')
    expect(http.calls[1].url.pathname).toBe(
      '/api/v1/skills/pdf-forms/versions/1.0.0/files/refs/api%20notes/%C3%B1.md',
    )
  })

  it('refuses to build URLs from unsafe parameters', async () => {
    const http = mockFetch([])
    expect(() => api.getEntry('a/b')).toThrow(TypeError)
    expect(() => api.getEntry('..')).toThrow(TypeError)
    expect(() => api.readFile('x', '1.0.0', '../../etc/passwd')).toThrow(TypeError)
    expect(() => api.readFile('x', '1.0.0', '/abs')).toThrow(TypeError)
    expect(() => api.revokeToken('')).toThrow(TypeError)
    expect(http.calls).toHaveLength(0)
  })

  it('uses the routes of the current API', async () => {
    signIn()
    const http = mockFetch([
      { method: 'GET', path: /.*/, respond: () => jsonResponse({}) },
      { method: 'POST', path: /.*/, respond: () => jsonResponse({}) },
      { method: 'DELETE', path: /.*/, respond: () => emptyResponse() },
    ])
    await api.hideEntry('e')
    await api.restoreEntry('e')
    await api.publishVersion('e', '1.0.0')
    await api.deprecateVersion('e', '1.0.0')
    await api.getVersion('e', 'latest')
    await api.listFiles('e', '1.0.0')
    await api.listTags({ limit: 10 })
    await api.listCategories()
    await api.revokeToken('tok-1')
    await api.listUsers()
    await api.createUser({ username: 'u', email: 'u@x.test', password: 'password1', role: 'publisher' })
    expect(http.calls.map((call) => `${call.method} ${call.path}`)).toEqual([
      'DELETE /skills/e',
      'POST /skills/e/restore',
      'POST /skills/e/versions/1.0.0/publish',
      'POST /skills/e/versions/1.0.0/deprecate',
      'GET /skills/e/versions/latest',
      'GET /skills/e/versions/1.0.0/files',
      'GET /tags',
      'GET /categories',
      'DELETE /tokens/tok-1',
      'GET /users',
      'POST /users',
    ])
  })
})

describe('response handling', () => {
  it('returns parsed JSON', async () => {
    mockFetch([{ method: 'GET', path: '/info', respond: () => jsonResponse({ name: 'Alexandria' }) }])
    expect(await api.info()).toEqual({ name: 'Alexandria' })
  })

  it('returns null for 204 without reading a body', async () => {
    const response = emptyResponse()
    const json = vi.spyOn(response, 'json')
    const text = vi.spyOn(response, 'text')
    signIn()
    mockFetch([{ method: 'DELETE', path: '/skills/e', respond: () => response }])
    expect(await api.hideEntry('e')).toBeNull()
    expect(json).not.toHaveBeenCalled()
    expect(text).not.toHaveBeenCalled()
  })

  it('does not assume JSON for downloads', async () => {
    signIn()
    mockFetch([
      {
        method: 'GET',
        path: '/skills/e/versions/1.0.0/download',
        respond: () => binaryResponse([80, 75, 3, 4], { 'x-content-sha256': 'abc123' }),
      },
      {
        method: 'GET',
        path: '/skills/e/versions/1.0.0/raw/a/b.bin',
        respond: () => binaryResponse([1, 2, 3]),
      },
    ])
    const { blob, sha256 } = await api.downloadPackage('e', '1.0.0')
    expect(blob.size).toBe(4)
    expect(sha256).toBe('abc123')
    const file = await api.downloadFile('e', '1.0.0', 'a/b.bin')
    expect(file.size).toBe(3)
  })

  it('reports a response that is not valid JSON', async () => {
    mockFetch([
      {
        method: 'GET',
        path: '/info',
        respond: () => new Response('<html>proxy page</html>', { status: 200 }),
      },
    ])
    await expect(api.info()).rejects.toMatchObject({ code: 'INVALID_RESPONSE', status: 200 })
  })

  it('passes the abort signal through and rethrows aborts untouched', async () => {
    const controller = new AbortController()
    const http = mockFetch([
      {
        method: 'GET',
        path: '/skills',
        respond: ({ signal }) =>
          new Promise((_, reject) => {
            signal.addEventListener('abort', () =>
              reject(new DOMException('aborted', 'AbortError')),
            )
          }),
      },
    ])
    const pending = api.listEntries({}, { signal: controller.signal })
    controller.abort()
    await expect(pending).rejects.toSatisfy((error) => isAbortError(error) && !isApiError(error))
    expect(http.calls[0].signal).toBe(controller.signal)
  })
})

describe('error normalization', () => {
  it('reads the error envelope of the API', async () => {
    mockFetch([
      {
        method: 'POST',
        path: '/skills/e/versions/1.0.0/deprecate',
        respond: () =>
          errorResponse(409, 'CONFLICT', 'The only published version cannot be deprecated'),
      },
    ])
    signIn()
    const error = await api.deprecateVersion('e', '1.0.0').catch((cause) => cause)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({
      status: 409,
      code: 'CONFLICT',
      message: 'The only published version cannot be deprecated',
      reason: null,
    })
  })

  it('keeps the reason of an invalid package', async () => {
    mockFetch([
      {
        method: 'GET',
        path: '/skills/e/versions/1.0.0/files/x',
        respond: () => errorResponse(422, 'VALIDATION_ERROR', 'Invalid path', 'INVALID_PATH'),
      },
    ])
    const error = await api.readFile('e', '1.0.0', 'x').catch((cause) => cause)
    expect(error).toMatchObject({ status: 422, code: 'VALIDATION_ERROR', reason: 'INVALID_PATH' })
  })

  it('falls back to a generic error for bodies that are not the envelope', () => {
    expect(errorFromParts(502, '<html>Bad gateway</html>', 'Bad Gateway')).toMatchObject({
      status: 502,
      code: 'HTTP_502',
      message: 'The request failed (502 Bad Gateway).',
      reason: null,
    })
    expect(errorFromParts(500, '', '')).toMatchObject({ code: 'HTTP_500', reason: null })
    expect(errorFromParts(422, JSON.stringify({ detail: 'old style' }))).toMatchObject({
      code: 'HTTP_422',
    })
    expect(errorFromParts(400, JSON.stringify({ error: { code: 'X' } }))).toMatchObject({
      code: 'X',
      message: 'The request failed (400).',
    })
  })

  it('reports network failures with status 0', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))
    const error = await api.info().catch((cause) => cause)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 0, code: 'NETWORK_ERROR' })
  })
})

describe('deactivateUser', () => {
  const spec = (paths) => ({ openapi: '3.1.0', paths })

  beforeEach(() => {
    resetRouteDiscovery()
    signIn(ADMIN, TOKEN)
  })

  it('calls the route that the server documents for deactivation', async () => {
    const http = mockFetch([
      {
        method: 'GET',
        path: '/openapi.json',
        respond: () =>
          jsonResponse(
            spec({
              '/api/v1/users': { get: { summary: 'List users' }, post: { summary: 'Create a user' } },
              '/api/v1/users/{user_id}/deactivate': { post: { summary: 'Deactivate a user' } },
            }),
          ),
      },
      { method: 'POST', path: '/users/u-1/deactivate', respond: () => jsonResponse({ id: 'u-1' }) },
    ])
    await expect(api.deactivateUser('u-1')).resolves.toEqual({ id: 'u-1' })
    expect(http.callsTo('POST', '/users/u-1/deactivate')).toHaveLength(1)
    expect(Object.values(http.callsTo('POST', '/users/u-1/deactivate')[0].headers)).toContain(`Bearer ${TOKEN}`)
  })

  it('follows another method and path when the server documents them', async () => {
    const http = mockFetch([
      {
        method: 'GET',
        path: '/openapi.json',
        respond: () => jsonResponse(spec({ '/api/v1/users/{id}': { delete: { operationId: 'deactivate_user', summary: 'Stop an account' } } })),
      },
      { method: 'DELETE', path: '/users/u%201', respond: () => emptyResponse() },
    ])
    await expect(api.deactivateUser('u 1')).resolves.toBeNull()
    expect(http.callsTo('DELETE', '/users/u%201')).toHaveLength(1)
  })

  it('never guesses a destructive route that is not about deactivation', async () => {
    const http = mockFetch([
      {
        method: 'GET',
        path: '/openapi.json',
        respond: () => jsonResponse(spec({ '/api/v1/users/{user_id}': { delete: { summary: 'Delete a user' } } })),
      },
    ])
    const error = await api.deactivateUser('u-1').catch((cause) => cause)
    expect(isMissingEndpoint(error)).toBe(true)
    expect(http.calls.filter((call) => call.method !== 'GET')).toHaveLength(0)
  })

  it('reports a server without any deactivation route without calling a made-up one', async () => {
    const http = mockFetch([
      {
        method: 'GET',
        path: '/openapi.json',
        respond: () => jsonResponse(spec({ '/api/v1/users': { get: { summary: 'List users' } } })),
      },
    ])
    await expect(api.deactivateUser('u-1')).rejects.toSatisfy(isMissingEndpoint)
    expect(http.calls.map((call) => call.path)).toEqual(['/openapi.json'])
  })

  it('does not call a made-up route when the document cannot be read', async () => {
    const http = mockFetch([
      { method: 'GET', path: '/openapi.json', respond: () => errorResponse(404, 'NOT_FOUND', 'Not Found') },
      { method: 'POST', path: '/users/u-1/deactivate', respond: () => jsonResponse({ id: 'u-1' }) },
    ])
    await expect(api.deactivateUser('u-1')).rejects.toSatisfy(isMissingEndpoint)
    expect(http.calls.map((call) => `${call.method} ${call.path}`)).toEqual(['GET /openapi.json'])
  })

  it('does not call a made-up route when the document cannot even be fetched', async () => {
    const http = mockFetch([
      {
        method: 'GET',
        path: '/openapi.json',
        respond: () => {
          throw new TypeError('Failed to fetch')
        },
      },
      { method: 'POST', path: /deactivate$/, respond: () => jsonResponse({}) },
    ])
    await expect(api.deactivateUser('u-1')).rejects.toSatisfy(isMissingEndpoint)
    expect(http.calls.filter((call) => call.method !== 'GET')).toHaveLength(0)
  })

  it('reads the document only once', async () => {
    const http = mockFetch([
      {
        method: 'GET',
        path: '/openapi.json',
        respond: () => jsonResponse(spec({ '/api/v1/users/{user_id}/deactivate': { post: { summary: 'Deactivate a user' } } })),
      },
      { method: 'POST', path: /deactivate$/, respond: () => jsonResponse({}) },
    ])
    await api.deactivateUser('a')
    await api.deactivateUser('b')
    expect(http.callsTo('GET', '/openapi.json')).toHaveLength(1)
  })

  it('still rejects ids that are unsafe in a path, before asking the server anything', async () => {
    const http = mockFetch([
      {
        method: 'GET',
        path: '/openapi.json',
        respond: () => jsonResponse(spec({ '/api/v1/users/{user_id}/deactivate': { post: { summary: 'Deactivate a user' } } })),
      },
    ])
    await expect(api.deactivateUser('../etc')).rejects.toThrow(TypeError)
    await expect(api.deactivateUser('a/b')).rejects.toThrow(TypeError)
    await expect(api.deactivateUser('')).rejects.toThrow(TypeError)
    expect(http.calls).toHaveLength(0)
  })
})

describe('checkUserDeactivation', () => {
  const DEACTIVATE = { '/api/v1/users/{user_id}/deactivate': { post: { summary: 'Deactivate a user' } } }
  const document = (paths) => jsonResponse({ openapi: '3.1.0', paths })

  beforeEach(() => {
    resetRouteDiscovery()
    signIn(ADMIN, TOKEN)
  })

  it('is available when the server documents a deactivation route', async () => {
    mockFetch([{ method: 'GET', path: '/openapi.json', respond: () => document(DEACTIVATE) }])
    await expect(api.checkUserDeactivation()).resolves.toBe('available')
  })

  it('is missing when the server documents users but no deactivation route', async () => {
    mockFetch([
      {
        method: 'GET',
        path: '/openapi.json',
        respond: () =>
          document({
            '/api/v1/users': { get: { summary: 'List users' }, post: { summary: 'Create a user' } },
            '/api/v1/users/{user_id}': { delete: { summary: 'Delete a user' } },
          }),
      },
    ])
    await expect(api.checkUserDeactivation()).resolves.toBe('missing')
  })

  it.each([
    ['an error status', () => errorResponse(500, 'INTERNAL_ERROR', 'x')],
    ['a missing document', () => errorResponse(404, 'NOT_FOUND', 'Not Found')],
    ['a body that is not JSON', () => new Response('<html>proxy page</html>', { status: 200 })],
    ['JSON that is not an API description', () => jsonResponse({ title: 'x' })],
    ['an API description without paths', () => jsonResponse({ openapi: '3.1.0', paths: null })],
    ['a list instead of an object', () => jsonResponse([])],
    [
      'a network failure',
      () => {
        throw new TypeError('Failed to fetch')
      },
    ],
  ])('is unknown, never available, after %s', async (_name, respond) => {
    const http = mockFetch([
      { method: 'GET', path: '/openapi.json', respond },
      { method: 'POST', path: /.*/, respond: () => jsonResponse({}) },
    ])
    await expect(api.checkUserDeactivation()).resolves.toBe('unknown')
    expect(http.calls.map((call) => `${call.method} ${call.path}`)).toEqual(['GET /openapi.json'])
  })

  it('does not send the session to the document', async () => {
    const http = mockFetch([{ method: 'GET', path: '/openapi.json', respond: () => document(DEACTIVATE) }])
    await api.checkUserDeactivation()
    expect(http.calls[0].url.pathname).toBe('/openapi.json')
    expect(http.calls[0].url.search).toBe('')
    expect(headersOf(http.calls[0]).authorization).toBeUndefined()
  })

  it('remembers a settled answer, so the document is read only once', async () => {
    const http = mockFetch([{ method: 'GET', path: '/openapi.json', respond: () => document(DEACTIVATE) }])
    await api.checkUserDeactivation()
    await api.checkUserDeactivation()
    await api.checkUserDeactivation()
    expect(http.callsTo('GET', '/openapi.json')).toHaveLength(1)
  })

  it('remembers that a route is missing too', async () => {
    const http = mockFetch([{ method: 'GET', path: '/openapi.json', respond: () => document({}) }])
    await expect(api.checkUserDeactivation()).resolves.toBe('missing')
    await expect(api.checkUserDeactivation()).resolves.toBe('missing')
    expect(http.callsTo('GET', '/openapi.json')).toHaveLength(1)
  })

  it('shares a single request between simultaneous checks and deactivations', async () => {
    const http = mockFetch([
      { method: 'GET', path: '/openapi.json', respond: () => document(DEACTIVATE) },
      { method: 'POST', path: '/users/a/deactivate', respond: () => jsonResponse({ id: 'a' }) },
    ])
    const answers = await Promise.all([
      api.checkUserDeactivation(),
      api.checkUserDeactivation(),
      api.deactivateUser('a'),
    ])
    expect(answers).toEqual(['available', 'available', { id: 'a' }])
    expect(http.callsTo('GET', '/openapi.json')).toHaveLength(1)
  })

  it('does not remember an unreadable document, so the next check tries again', async () => {
    let readable = false
    const http = mockFetch([
      {
        method: 'GET',
        path: '/openapi.json',
        respond: () => (readable ? document(DEACTIVATE) : errorResponse(503, 'UNAVAILABLE', 'later')),
      },
    ])
    await expect(api.checkUserDeactivation()).resolves.toBe('unknown')
    readable = true
    await expect(api.checkUserDeactivation()).resolves.toBe('available')
    await expect(api.checkUserDeactivation()).resolves.toBe('available')
    expect(http.callsTo('GET', '/openapi.json')).toHaveLength(2)
  })

  it('reads the document again after the discovery is reset', async () => {
    const http = mockFetch([{ method: 'GET', path: '/openapi.json', respond: () => document(DEACTIVATE) }])
    await api.checkUserDeactivation()
    resetRouteDiscovery()
    await api.checkUserDeactivation()
    expect(http.callsTo('GET', '/openapi.json')).toHaveLength(2)
  })

  it('does not let a late unreadable answer erase a newer discovery', async () => {
    let release
    let reads = 0
    const http = mockFetch([
      {
        method: 'GET',
        path: '/openapi.json',
        respond: () => {
          reads += 1
          if (reads > 1) return document(DEACTIVATE)
          return new Promise((resolve) => {
            release = () => resolve(errorResponse(503, 'UNAVAILABLE', 'late'))
          })
        },
      },
    ])
    const stale = api.checkUserDeactivation()
    resetRouteDiscovery()
    await expect(api.checkUserDeactivation()).resolves.toBe('available')
    release()
    await expect(stale).resolves.toBe('unknown')
    await expect(api.checkUserDeactivation()).resolves.toBe('available')
    expect(http.callsTo('GET', '/openapi.json')).toHaveLength(2)
  })
})

describe('isMissingEndpoint', () => {
  it('recognizes routes that the server does not have', () => {
    expect(isMissingEndpoint(new ApiError({ status: 405, code: 'METHOD_NOT_ALLOWED', message: 'Method Not Allowed' }))).toBe(true)
    expect(isMissingEndpoint(new ApiError({ status: 501, code: 'NOT_IMPLEMENTED', message: 'x' }))).toBe(true)
    expect(isMissingEndpoint(new ApiError({ status: 404, code: 'NOT_FOUND', message: 'Not Found' }))).toBe(true)
  })

  it('does not mistake a missing resource for a missing route', () => {
    expect(isMissingEndpoint(new ApiError({ status: 404, code: 'NOT_FOUND', message: 'User not found' }))).toBe(false)
    expect(isMissingEndpoint(new ApiError({ status: 409, code: 'CONFLICT', message: 'Not Found' }))).toBe(false)
    expect(isMissingEndpoint(new Error('Not Found'))).toBe(false)
    expect(isMissingEndpoint(null)).toBe(false)
  })
})

describe('401 handling', () => {
  it('ends the session and goes back to the login page', async () => {
    signIn(ADMIN, TOKEN)
    router.navigate('/library/pdf-forms', { query: { tab: 'versions' } })
    mockFetch([
      {
        method: 'GET',
        path: '/tokens',
        respond: () => errorResponse(401, 'UNAUTHORIZED', 'The credential is invalid'),
      },
    ])
    await expect(api.listTokens()).rejects.toMatchObject({ status: 401, code: 'UNAUTHORIZED' })
    expect(session.isAuthenticated).toBe(false)
    expect(session.token).toBeNull()
    expect(window.sessionStorage.getItem('alexandria.admin.session')).toBeNull()
    expect(session.notice).toMatch(/expired/)
    expect(router.route.name).toBe('login')
    expect(router.route.query.get('next')).toBe('/library/pdf-forms?tab=versions')
  })

  it('handles a burst of simultaneous 401 responses once', async () => {
    signIn(ADMIN, TOKEN)
    router.navigate('/library')
    mockFetch([
      {
        method: 'GET',
        path: /.*/,
        respond: () => errorResponse(401, 'UNAUTHORIZED', 'expired'),
      },
    ])
    const results = await Promise.allSettled([api.listEntries(), api.listTags(), api.listTokens()])
    expect(results.every((item) => item.status === 'rejected')).toBe(true)
    expect(router.route.name).toBe('login')
    expect(router.route.query.get('next')).toBe('/library')
  })

  it('does not log out a newer session because of a stale request', async () => {
    signIn(ADMIN, 'old-token')
    let release
    mockFetch([
      {
        method: 'GET',
        path: '/tokens',
        respond: () =>
          new Promise((resolve) => {
            release = () => resolve(errorResponse(401, 'UNAUTHORIZED', 'expired'))
          }),
      },
    ])
    const pending = api.listTokens().catch((cause) => cause)
    signIn(ADMIN, 'new-token')
    release()
    await pending
    expect(session.token).toBe('new-token')
  })

  it('treats a wrong password as a plain error, not as an expired session', async () => {
    signIn(ADMIN, TOKEN)
    mockFetch([
      {
        method: 'POST',
        path: '/auth/login',
        respond: () => errorResponse(401, 'UNAUTHORIZED', 'Invalid credentials'),
      },
    ])
    await expect(api.login('ada', 'wrong')).rejects.toMatchObject({ status: 401 })
    expect(session.isAuthenticated).toBe(true)
  })

  it('does not redirect on other statuses', async () => {
    signIn(ADMIN, TOKEN)
    router.navigate('/tokens')
    mockFetch([
      { method: 'GET', path: '/tokens', respond: () => errorResponse(403, 'FORBIDDEN', 'no') },
    ])
    await expect(api.listTokens()).rejects.toMatchObject({ status: 403 })
    expect(session.isAuthenticated).toBe(true)
    expect(router.route.name).toBe('tokens')
  })
})

describe('login', () => {
  it('posts the credentials as JSON without any Authorization header', async () => {
    signIn(ADMIN, TOKEN)
    const http = mockFetch([
      {
        method: 'POST',
        path: '/auth/login',
        respond: () => jsonResponse({ token: 'new', user: ADMIN }),
      },
    ])
    const result = await api.login('ada', 's3cret')
    expect(result.token).toBe('new')
    const call = http.calls[0]
    expect(JSON.parse(call.body)).toEqual({ username: 'ada', password: 's3cret' })
    expect(headersOf(call).authorization).toBeUndefined()
    expect(call.url.search).toBe('')
  })
})

describe('uploadPackage', () => {
  let xhr

  function startUpload(options = {}, file = new File(['zip-bytes'], 'pkg.zip', { type: 'application/zip' })) {
    const promise = api.uploadPackage(file, options)
    xhr = FakeXHR.instances.at(-1)
    return promise
  }

  beforeEach(() => {
    installFakeXHR()
    signIn(ADMIN, TOKEN)
  })

  it('posts multipart form data to the packages endpoint with the bearer header', async () => {
    const promise = startUpload({ version: '1.2.3', publish: true })
    expect(xhr.method).toBe('POST')
    expect(xhr.url).toBe('/api/v1/packages')
    expect(xhr.headers.Authorization).toBe(`Bearer ${TOKEN}`)
    expect(xhr.headers.Accept).toBe('application/json')
    expect(xhr.headers['Content-Type']).toBeUndefined()
    expect(xhr.url).not.toContain(TOKEN)
    expect(xhr.sent).toBeInstanceOf(FormData)
    expect(xhr.sent.get('file').name).toBe('pkg.zip')
    expect(xhr.sent.get('version')).toBe('1.2.3')
    expect(xhr.sent.get('publish')).toBe('true')
    xhr.respond(201, { skill: { name: 'x' }, version: { version: '1.2.3' } })
    await expect(promise).resolves.toEqual({
      entry: { name: 'x' },
      version: { version: '1.2.3' },
    })
  })

  it('sends publish=false explicitly and omits an empty version', async () => {
    const promise = startUpload({ publish: false })
    expect(xhr.sent.get('publish')).toBe('false')
    expect(xhr.sent.has('version')).toBe(false)
    xhr.respond(201, { skill: {}, version: {} })
    await promise
  })

  it('omits publish when it is not specified', async () => {
    const promise = startUpload()
    expect(xhr.sent.has('publish')).toBe(false)
    xhr.respond(201, { skill: {}, version: {} })
    await promise
  })

  it('reports real progress and the end of the transfer', async () => {
    const events = []
    const promise = startUpload({ onProgress: (progress) => events.push(progress) })
    xhr.progress(3, 9)
    xhr.progress(9, 9)
    xhr.uploaded()
    xhr.respond(201, { skill: {}, version: {} })
    await promise
    expect(events).toEqual([
      { loaded: 3, total: 9, done: false },
      { loaded: 9, total: 9, done: false },
      { loaded: 9, total: 9, done: true },
    ])
  })

  it('normalizes server errors, keeping the reason', async () => {
    const promise = startUpload()
    xhr.respond(
      422,
      { error: { code: 'INVALID_PACKAGE', message: 'No entry point', reason: 'MISSING_ENTRYPOINT' } },
      'Unprocessable Entity',
    )
    await expect(promise).rejects.toMatchObject({
      status: 422,
      code: 'INVALID_PACKAGE',
      message: 'No entry point',
      reason: 'MISSING_ENTRYPOINT',
    })
  })

  it('reports a 413 and a response without the envelope', async () => {
    const first = startUpload()
    xhr.respond(413, { error: { code: 'PAYLOAD_TOO_LARGE', message: 'too big' } })
    await expect(first).rejects.toMatchObject({ status: 413, code: 'PAYLOAD_TOO_LARGE' })
    const second = startUpload()
    xhr.respond(502, '<html>bad gateway</html>')
    await expect(second).rejects.toMatchObject({ status: 502, code: 'HTTP_502' })
  })

  it('reports network failures', async () => {
    const promise = startUpload()
    xhr.fail()
    await expect(promise).rejects.toMatchObject({ status: 0, code: 'NETWORK_ERROR' })
  })

  it('can be cancelled with an abort signal', async () => {
    const controller = new AbortController()
    const promise = startUpload({ signal: controller.signal })
    controller.abort()
    expect(xhr.aborted).toBe(true)
    await expect(promise).rejects.toSatisfy(isAbortError)
  })

  it('rejects immediately when the signal is already aborted', async () => {
    const controller = new AbortController()
    controller.abort()
    await expect(api.uploadPackage(new File(['x'], 'a.zip'), { signal: controller.signal })).rejects.toSatisfy(
      isAbortError,
    )
  })

  it('ends the session on 401', async () => {
    router.navigate('/upload')
    const promise = startUpload()
    xhr.respond(401, { error: { code: 'UNAUTHORIZED', message: 'expired' } })
    await expect(promise).rejects.toMatchObject({ status: 401 })
    expect(session.isAuthenticated).toBe(false)
    expect(router.route.name).toBe('login')
  })

  it('rejects an unreadable success response', async () => {
    const promise = startUpload()
    xhr.respond(201, 'not json')
    await expect(promise).rejects.toMatchObject({ code: 'INVALID_RESPONSE' })
  })
})
