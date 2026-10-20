import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { router, setPageTitle } from './router.svelte.js'
import {
  ROUTES,
  entryHref,
  hrefFor,
  loginQuery,
  matchPattern,
  parseHash,
  paths,
  queryString,
  resolveHash,
  safeNext,
} from './routes.js'

describe('parseHash', () => {
  it('splits the path from the query', () => {
    const { path, query } = parseHash('#/library/pdf-forms?tab=versions&tag=a&tag=b')
    expect(path).toBe('/library/pdf-forms')
    expect(query.get('tab')).toBe('versions')
    expect(query.getAll('tag')).toEqual(['a', 'b'])
  })

  it('normalizes empty hashes, missing slashes and trailing slashes', () => {
    expect(parseHash('').path).toBe('/')
    expect(parseHash('#').path).toBe('/')
    expect(parseHash('#/').path).toBe('/')
    expect(parseHash('#library').path).toBe('/library')
    expect(parseHash('#/library/').path).toBe('/library')
    expect(parseHash('#/library///').path).toBe('/library')
  })
})

describe('matchPattern', () => {
  it('captures and decodes parameters', () => {
    expect(matchPattern('/library/:name', '/library/pdf%20forms')).toEqual({ name: 'pdf forms' })
    expect(matchPattern('/library/:name', '/library/%C3%B1andu')).toEqual({ name: 'ñandu' })
  })

  it('does not match other shapes', () => {
    expect(matchPattern('/library/:name', '/library')).toBeNull()
    expect(matchPattern('/library', '/library/x')).toBeNull()
    expect(matchPattern('/upload', '/tokens')).toBeNull()
  })

  it('refuses malformed percent-encoding', () => {
    expect(matchPattern('/library/:name', '/library/%E0%A4%A')).toBeNull()
  })
})

describe('resolveHash', () => {
  it.each([
    ['#/', 'dashboard'],
    ['', 'dashboard'],
    ['#/dashboard', 'dashboard'],
    ['#/library', 'library'],
    ['#/library/pdf-forms', 'entry'],
    ['#/upload', 'upload'],
    ['#/tokens', 'tokens'],
    ['#/users', 'users'],
    ['#/login', 'login'],
    ['#/nothing-here', 'not-found'],
    ['#/library/a/b', 'not-found'],
  ])('maps %s to %s', (hash, name) => {
    expect(resolveHash(hash).name).toBe(name)
  })

  it('exposes params, query and a canonical hash', () => {
    const route = resolveHash('#/library/pdf-forms?tab=versions')
    expect(route.params).toEqual({ name: 'pdf-forms' })
    expect(route.query.get('tab')).toBe('versions')
    expect(route.hash).toBe('/library/pdf-forms?tab=versions')
    expect(route.admin).toBe(false)
  })

  it('flags the admin-only and anonymous routes', () => {
    expect(resolveHash('#/users').admin).toBe(true)
    expect(resolveHash('#/login').anonymous).toBe(true)
    expect(resolveHash('#/library').anonymous).toBe(false)
  })

  it('has a title for every route', () => {
    for (const route of ROUTES) expect(route.title.length).toBeGreaterThan(2)
  })
})

describe('queryString and hrefFor', () => {
  it('skips empty values and repeats arrays', () => {
    expect(queryString({ q: 'a b', tag: ['x', 'y'], kind: '', page: undefined, mine: true })).toBe(
      '?q=a+b&tag=x&tag=y&mine=1',
    )
    expect(queryString({ mine: false, x: null })).toBe('')
    expect(queryString(null)).toBe('')
    expect(queryString(new URLSearchParams('a=1&a=2'))).toBe('?a=1&a=2')
    expect(queryString([['a', 1], ['b', '']])).toBe('?a=1')
  })

  it('builds hash links with encoded path parameters', () => {
    expect(hrefFor(paths.entry('pdf forms'), { tab: 'files' })).toBe(
      '#/library/pdf%20forms?tab=files',
    )
    expect(hrefFor(paths.library())).toBe('#/library')
    expect(() => paths.entry('a/b')).toThrow(TypeError)
    expect(() => paths.entry('..')).toThrow(TypeError)
  })

  it('round-trips a name with special characters', () => {
    const name = 'wei rd?name#1'
    const resolved = resolveHash(hrefFor(paths.entry(name)))
    expect(resolved.params.name).toBe(name)
  })
})

describe('entryHref', () => {
  it('links an entry by name with an optional query', () => {
    expect(entryHref('pdf-forms')).toBe('#/library/pdf-forms')
    expect(entryHref('pdf forms', { tab: 'files', version: '1.0.0' })).toBe(
      '#/library/pdf%20forms?tab=files&version=1.0.0',
    )
  })

  it('never throws, even for a name that cannot be a path segment', () => {
    expect(entryHref('a/b')).toBe('#/library?q=a%2Fb')
    expect(entryHref('..')).toBe('#/library?q=..')
    expect(entryHref(/** @type {any} */ (undefined))).toBe('#/library?q=undefined')
  })
})

describe('loginQuery', () => {
  it('remembers where the user wanted to go', () => {
    expect(loginQuery('/library?tag=a')).toEqual({ next: '/library?tag=a' })
    expect(loginQuery('/library/pdf-forms?tab=files')).toEqual({ next: '/library/pdf-forms?tab=files' })
    expect(loginQuery('/tokens')).toEqual({ next: '/tokens' })
  })

  it('does not remember the dashboard, the login page or unknown routes', () => {
    expect(loginQuery('/')).toBeNull()
    expect(loginQuery('/dashboard')).toBeNull()
    expect(loginQuery('/login?next=%2Ftokens')).toBeNull()
    expect(loginQuery('/nowhere')).toBeNull()
  })
})

describe('safeNext', () => {
  it('keeps in-app routes', () => {
    expect(safeNext('/library?tag=a')).toBe('/library?tag=a')
    expect(safeNext('/library/pdf-forms?tab=versions')).toBe('/library/pdf-forms?tab=versions')
    expect(safeNext('/upload')).toBe('/upload')
  })

  it('falls back to the dashboard for anything suspicious', () => {
    for (const value of [
      null,
      undefined,
      '',
      'library',
      '//evil.example',
      'https://evil.example',
      'javascript:alert(1)',
      '/\\evil',
      '/login',
      '/does-not-exist',
      '/library\u0000',
    ]) {
      expect(safeNext(value)).toBe('/')
    }
  })
})

describe('router', () => {
  beforeEach(() => {
    router.stop()
    window.history.replaceState(null, '', '#/')
    router.start()
  })

  afterEach(() => {
    router.stop()
  })

  it('starts on the current location', () => {
    expect(router.route.name).toBe('dashboard')
  })

  it('navigates synchronously and updates the address', () => {
    router.navigate(paths.library(), { query: { q: 'pdf', tag: ['a', 'b'] } })
    expect(router.route.name).toBe('library')
    expect(router.route.query.get('q')).toBe('pdf')
    expect(window.location.hash).toBe('#/library?q=pdf&tag=a&tag=b')
  })

  it('pushes a history entry on navigate and none on replace', () => {
    const before = window.history.length
    router.navigate(paths.tokens())
    expect(window.history.length).toBe(before + 1)
    router.navigate(paths.upload(), { replace: true })
    expect(window.history.length).toBe(before + 1)
    expect(router.route.name).toBe('upload')
  })

  it('reacts to hashchange events', async () => {
    window.location.hash = '#/users'
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(router.route.name).toBe('users')
  })

  it('stops listening when stopped', async () => {
    router.stop()
    window.location.hash = '#/tokens'
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(router.route.name).not.toBe('tokens')
  })

  it('reports unknown routes', () => {
    router.navigate('/missing')
    expect(router.route.name).toBe('not-found')
    expect(router.route.path).toBe('/missing')
  })
})

describe('setPageTitle', () => {
  it('adds the product suffix', () => {
    setPageTitle('Library')
    expect(document.title).toBe('Library · Alexandria Admin')
    setPageTitle()
    expect(document.title).toBe('Alexandria Admin')
  })
})
