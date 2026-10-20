import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ADMIN, PUBLISHER, resetApp } from '../test/helpers.js'
import { session } from './auth.svelte.js'

const KEY = 'alexandria.admin.session'

beforeEach(resetApp)
afterEach(resetApp)

describe('session', () => {
  it('starts signed out', () => {
    expect(session.isAuthenticated).toBe(false)
    expect(session.user).toBeNull()
    expect(session.isAdmin).toBe(false)
  })

  it('stores the JWT and the user in sessionStorage, never in localStorage', () => {
    session.start('the-jwt', { ...ADMIN })
    expect(session.isAuthenticated).toBe(true)
    expect(session.isAdmin).toBe(true)
    const stored = JSON.parse(window.sessionStorage.getItem(KEY))
    expect(stored.token).toBe('the-jwt')
    expect(stored.user.username).toBe(ADMIN.username)
    expect(window.localStorage.length).toBe(0)
    expect(JSON.stringify(window.localStorage)).not.toContain('the-jwt')
  })

  it('distinguishes admins from publishers', () => {
    session.start('t', { ...PUBLISHER })
    expect(session.isAdmin).toBe(false)
  })

  it('clears everything on end and can leave a notice for the login page', () => {
    session.start('the-jwt', { ...ADMIN })
    session.end('Your session has expired.')
    expect(session.isAuthenticated).toBe(false)
    expect(session.user).toBeNull()
    expect(session.notice).toBe('Your session has expired.')
    expect(window.sessionStorage.getItem(KEY)).toBeNull()
    session.clearNotice()
    expect(session.notice).toBe('')
  })

  it('clears the notice when a new session starts', () => {
    session.end('Expired')
    session.start('t', { ...ADMIN })
    expect(session.notice).toBe('')
  })

  it('restores a stored session', () => {
    window.sessionStorage.setItem(KEY, JSON.stringify({ token: 'stored', user: { ...PUBLISHER } }))
    session.restore()
    expect(session.token).toBe('stored')
    expect(session.user?.username).toBe(PUBLISHER.username)
  })

  it('ignores corrupt or incomplete stored data', () => {
    for (const raw of ['not json', '{}', '{"token":"x"}', '{"token":"","user":{"id":"1","username":"a"}}', 'null']) {
      window.sessionStorage.setItem(KEY, raw)
      session.restore()
      expect(session.isAuthenticated).toBe(false)
    }
  })

  it('keeps working when storage is unavailable', () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError')
    })
    expect(() => session.start('t', { ...ADMIN })).not.toThrow()
    expect(session.isAuthenticated).toBe(true)
    expect(() => session.restore()).not.toThrow()
    setItem.mockRestore()
    getItem.mockRestore()
  })

  it('updates the stored user without touching the token', () => {
    session.start('t', { ...PUBLISHER })
    session.updateUser({ ...PUBLISHER, role: 'admin' })
    expect(session.isAdmin).toBe(true)
    expect(JSON.parse(window.sessionStorage.getItem(KEY))).toMatchObject({ token: 't' })
  })
})
