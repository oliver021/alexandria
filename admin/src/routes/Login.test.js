import { render, screen, waitFor } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { session } from '../lib/auth.svelte.js'
import { router } from '../lib/router.svelte.js'
import {
  ADMIN,
  errorResponse,
  jsonResponse,
  mockFetch,
  resetApp,
  scrolledIntoView,
} from '../test/helpers.js'
import Login from './Login.svelte'

beforeEach(() => {
  resetApp()
  router.stop()
  router.start()
})

afterEach(() => {
  router.stop()
  resetApp()
})

async function submit(user, username = 'ada', password = 'correct horse') {
  await user.type(screen.getByLabelText('Username'), username)
  await user.type(screen.getByLabelText('Password'), password)
  await user.click(screen.getByRole('button', { name: 'Sign in' }))
}

describe('Login', () => {
  it('has labelled fields with the right autocomplete hints', () => {
    render(Login)
    expect(screen.getByLabelText('Username')).toHaveAttribute('autocomplete', 'username')
    expect(screen.getByLabelText('Password')).toHaveAttribute('type', 'password')
    expect(screen.getByLabelText('Password')).toHaveAttribute('autocomplete', 'current-password')
  })

  it('starts the session and goes to the dashboard', async () => {
    const http = mockFetch([
      { method: 'POST', path: '/auth/login', respond: () => jsonResponse({ token: 'new-jwt', user: ADMIN }) },
    ])
    const user = userEvent.setup()
    router.navigate('/login')
    render(Login)
    await submit(user)
    await waitFor(() => expect(session.isAuthenticated).toBe(true))
    expect(JSON.parse(http.calls[0].body)).toEqual({ username: 'ada', password: 'correct horse' })
    expect(session.token).toBe('new-jwt')
    expect(session.user?.username).toBe('ada')
    expect(window.sessionStorage.getItem('alexandria.admin.session')).toContain('new-jwt')
    expect(window.localStorage.length).toBe(0)
    expect(router.route.name).toBe('dashboard')
  })

  it('returns to the page that was requested before signing in', async () => {
    mockFetch([
      { method: 'POST', path: '/auth/login', respond: () => jsonResponse({ token: 't', user: ADMIN }) },
    ])
    const user = userEvent.setup()
    router.navigate('/login', { query: { next: '/library/pdf-forms?tab=versions' } })
    render(Login)
    await submit(user)
    await waitFor(() => expect(router.route.name).toBe('entry'))
    expect(router.route.params.name).toBe('pdf-forms')
    expect(router.route.query.get('tab')).toBe('versions')
  })

  it('ignores an unsafe next parameter', async () => {
    mockFetch([
      { method: 'POST', path: '/auth/login', respond: () => jsonResponse({ token: 't', user: ADMIN }) },
    ])
    const user = userEvent.setup()
    router.navigate('/login', { query: { next: '//evil.example/steal' } })
    render(Login)
    await submit(user)
    await waitFor(() => expect(session.isAuthenticated).toBe(true))
    expect(router.route.name).toBe('dashboard')
  })

  it('explains wrong credentials and clears the password', async () => {
    mockFetch([
      {
        method: 'POST',
        path: '/auth/login',
        respond: () => errorResponse(401, 'UNAUTHORIZED', 'Invalid credentials'),
      },
    ])
    const user = userEvent.setup()
    render(Login)
    await submit(user, 'ada', 'wrong password')
    expect(await screen.findByRole('alert')).toHaveTextContent('The username or password is not correct.')
    expect(screen.getByLabelText('Password')).toHaveValue('')
    expect(screen.getByLabelText('Username')).toHaveValue('ada')
    expect(session.isAuthenticated).toBe(false)
  })

  it('reports network and server problems differently from bad credentials', async () => {
    mockFetch([
      { method: 'POST', path: '/auth/login', respond: () => errorResponse(500, 'INTERNAL_ERROR', 'x') },
    ])
    const user = userEvent.setup()
    render(Login)
    await submit(user)
    expect(await screen.findByRole('alert')).toHaveTextContent(/unexpected error/)
  })

  it.each([
    ['wrong credentials', () => errorResponse(401, 'UNAUTHORIZED', 'Invalid credentials')],
    ['a server problem', () => errorResponse(500, 'INTERNAL_ERROR', 'x')],
  ])('moves the focus to the explanation and scrolls it into view after %s', async (_name, respond) => {
    mockFetch([{ method: 'POST', path: '/auth/login', respond }])
    const user = userEvent.setup()
    render(Login)
    await submit(user)
    const alert = await screen.findByRole('alert')
    await waitFor(() => expect(alert).toHaveFocus())
    expect(alert).toHaveAttribute('id', 'login-error')
    expect(scrolledIntoView()).toEqual([{ element: alert, options: { block: 'center', inline: 'nearest' } }])
  })

  it('shows why the session ended', () => {
    session.end('Your session has expired. Sign in again.')
    render(Login)
    expect(screen.getByRole('status')).toHaveTextContent('Your session has expired. Sign in again.')
  })

  it('does not submit empty fields', async () => {
    const http = mockFetch([])
    const user = userEvent.setup()
    render(Login)
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(http.calls).toHaveLength(0)
  })

  it('disables the button while the request is running', async () => {
    let release
    mockFetch([
      {
        method: 'POST',
        path: '/auth/login',
        respond: () =>
          new Promise((resolve) => {
            release = () => resolve(jsonResponse({ token: 't', user: ADMIN }))
          }),
      },
    ])
    const user = userEvent.setup()
    render(Login)
    await submit(user)
    expect(await screen.findByRole('button', { name: /Signing in/ })).toBeDisabled()
    release()
    await waitFor(() => expect(session.isAuthenticated).toBe(true))
  })
})
