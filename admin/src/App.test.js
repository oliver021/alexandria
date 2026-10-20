import { render, screen, waitFor, within } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App.svelte'
import { session } from './lib/auth.svelte.js'
import { router } from './lib/router.svelte.js'
import {
  ADMIN,
  PUBLISHER,
  errorResponse,
  jsonResponse,
  mockFetch,
  registryInfo,
  resetApp,
  signIn,
} from './test/helpers.js'

function setup({ hash = '#/', me = ADMIN, extra = [] } = {}) {
  window.history.replaceState(null, '', hash)
  const http = mockFetch([
    ...extra,
    { method: 'GET', path: '/auth/me', respond: () => jsonResponse(me) },
    {
      method: 'GET',
      path: '/skills',
      respond: () => jsonResponse({ items: [], total: 0, limit: 1, offset: 0 }),
    },
    { method: 'GET', path: '/info', respond: () => jsonResponse(registryInfo()) },
    { method: 'GET', path: '/tags', respond: () => jsonResponse([]) },
    { method: 'GET', path: '/categories', respond: () => jsonResponse([]) },
    { method: 'GET', path: '/tokens', respond: () => jsonResponse([]) },
    { method: 'GET', path: '/users', respond: () => jsonResponse([ADMIN]) },
  ])
  return { http, user: userEvent.setup() }
}

beforeEach(() => {
  resetApp()
})

afterEach(() => {
  router.stop()
  resetApp()
})

const nav = () => screen.getByRole('navigation', { name: 'Main' })

describe('App: signed out', () => {
  it('sends an anonymous visitor to the login page and remembers where they wanted to go', async () => {
    setup({ hash: '#/library/pdf-forms?tab=versions' })
    render(App)
    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    await waitFor(() => expect(router.route.name).toBe('login'))
    expect(router.route.query.get('next')).toBe('/library/pdf-forms?tab=versions')
    expect(screen.queryByRole('navigation', { name: 'Main' })).not.toBeInTheDocument()
    expect(document.title).toBe('Sign in · Alexandria Admin')
  })

  it('does not remember the dashboard as a destination', async () => {
    setup({ hash: '#/' })
    render(App)
    await waitFor(() => expect(router.route.name).toBe('login'))
    expect(window.location.hash).toBe('#/login')
  })

  it('makes no authenticated request before signing in', async () => {
    const { http } = setup({ hash: '#/tokens' })
    render(App)
    await screen.findByRole('button', { name: 'Sign in' })
    expect(http.calls.filter((call) => call.path !== '/info')).toHaveLength(0)
  })

  it('signs in through the form and shows the dashboard', async () => {
    const { http, user } = setup({
      hash: '#/login',
      extra: [
        {
          method: 'POST',
          path: '/auth/login',
          respond: () => jsonResponse({ token: 'fresh-jwt', user: ADMIN }),
        },
      ],
    })
    render(App)
    await user.type(await screen.findByLabelText('Username'), 'ada')
    await user.type(screen.getByLabelText('Password'), 'correct horse')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Dashboard' })).toBeInTheDocument()
    expect(within(nav()).getByRole('link', { name: 'Dashboard' })).toHaveAttribute('aria-current', 'page')
    expect(document.title).toBe('Dashboard · Alexandria Admin')
    const authorized = http.calls.filter((call) => Object.values(call.headers).includes('Bearer fresh-jwt'))
    expect(authorized.length).toBeGreaterThan(0)
  })

  it('shows a notice when the session has expired', async () => {
    signIn(ADMIN, 'stale-jwt')
    const { user } = setup({
      hash: '#/tokens',
      extra: [
        {
          method: 'GET',
          path: '/tokens',
          respond: () => errorResponse(401, 'UNAUTHORIZED', 'The credential is invalid, expired or revoked'),
        },
        { method: 'GET', path: '/auth/me', respond: () => errorResponse(401, 'UNAUTHORIZED', 'expired') },
      ],
    })
    render(App)
    expect(await screen.findByText('Your session has expired. Sign in again.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(session.isAuthenticated).toBe(false)
    expect(router.route.name).toBe('login')
    expect(router.route.query.get('next')).toBe('/tokens')
    expect(user).toBeTruthy()
  })
})

describe('App: signed in', () => {
  beforeEach(() => signIn(ADMIN))

  it('lets a signed-in user skip the login page', async () => {
    setup({ hash: '#/login' })
    render(App)
    expect(await screen.findByRole('heading', { level: 1, name: 'Dashboard' })).toBeInTheDocument()
    expect(router.route.name).toBe('dashboard')
  })

  it('offers the user management link only to admins', async () => {
    setup()
    render(App)
    await screen.findByRole('heading', { level: 1, name: 'Dashboard' })
    expect(within(nav()).getByRole('link', { name: 'Users' })).toHaveAttribute('href', '#/users')
    expect(screen.getByText('ada')).toBeInTheDocument()
    expect(screen.getByText('Admin', { selector: '.badge' })).toBeInTheDocument()
  })

  it('hides it from publishers', async () => {
    resetApp()
    signIn(PUBLISHER)
    setup({ me: PUBLISHER })
    render(App)
    await screen.findByRole('heading', { level: 1, name: 'Dashboard' })
    expect(within(nav()).queryByRole('link', { name: 'Users' })).not.toBeInTheDocument()
    expect(within(nav()).getAllByRole('link').map((link) => link.textContent.trim())).toEqual([
      'Dashboard',
      'Library',
      'Upload',
      'API tokens',
    ])
  })

  it('shows a page for admins only when a publisher types the address', async () => {
    resetApp()
    signIn(PUBLISHER)
    setup({ hash: '#/users', me: PUBLISHER })
    render(App)
    expect(await screen.findByText('Admins only')).toBeInTheDocument()
  })

  it('refreshes the account from the server at startup', async () => {
    setup({ me: { ...ADMIN, role: 'publisher' } })
    render(App)
    await screen.findByRole('heading', { level: 1, name: 'Dashboard' })
    await waitFor(() => expect(session.isAdmin).toBe(false))
    await waitFor(() => expect(within(nav()).queryByRole('link', { name: 'Users' })).not.toBeInTheDocument())
  })

  it.each([
    ['#/library', 'Library', 'Library'],
    ['#/upload', 'Upload package', 'Upload'],
    ['#/tokens', 'API tokens', 'API tokens'],
    ['#/users', 'Users', 'Users'],
  ])('renders %s and marks it as the current page', async (hash, heading, navName) => {
    setup({ hash })
    render(App)
    expect(await screen.findByRole('heading', { level: 1, name: heading })).toBeInTheDocument()
    expect(within(nav()).getByRole('link', { name: navName })).toHaveAttribute('aria-current', 'page')
    expect(document.title).toBe(`${heading} · Alexandria Admin`)
  })

  it('shows a page that explains an unknown address', async () => {
    setup({ hash: '#/nowhere' })
    render(App)
    expect(await screen.findByRole('heading', { level: 1, name: 'Page not found' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Go to the dashboard' })).toHaveAttribute('href', '#/')
  })

  it('navigates between pages with the main navigation', async () => {
    const { user } = setup()
    render(App)
    await screen.findByRole('heading', { level: 1, name: 'Dashboard' })
    await user.click(within(nav()).getByRole('link', { name: 'Library' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Library' })).toBeInTheDocument()
    expect(window.location.hash).toBe('#/library')
    await user.click(within(nav()).getByRole('link', { name: 'API tokens' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'API tokens' })).toBeInTheDocument()
  })

  it('signs out and clears the session', async () => {
    const { user } = setup()
    render(App)
    await screen.findByRole('heading', { level: 1, name: 'Dashboard' })
    await user.click(screen.getByRole('button', { name: 'Sign out' }))
    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(session.isAuthenticated).toBe(false)
    expect(window.sessionStorage.getItem('alexandria.admin.session')).toBeNull()
    expect(screen.queryByRole('navigation', { name: 'Main' })).not.toBeInTheDocument()
  })
})

describe('App: unexpected failures', () => {
  beforeEach(() => signIn(ADMIN))

  it('contains a rendering error to the page and offers a retry', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    let malformed = true
    const { user } = setup({
      hash: '#/library',
      extra: [
        {
          method: 'GET',
          path: '/skills',
          respond: () =>
            jsonResponse(
              malformed
                ? { items: [{ id: 'x', name: 'x', display_name: 'X', description: 'd', kind: 'skill', status: 'published' }], total: 1, limit: 20, offset: 0 }
                : { items: [], total: 0, limit: 20, offset: 0 },
            ),
        },
      ],
    })
    render(App)
    expect(await screen.findByText('This page failed to display')).toBeInTheDocument()
    expect(screen.getByText(/unexpected error occurred/)).toBeInTheDocument()
    expect(within(nav()).getByRole('link', { name: 'Dashboard' })).toBeInTheDocument()
    expect(consoleError).toHaveBeenCalled()
    malformed = false
    await user.click(screen.getByRole('button', { name: /Try again/ }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Library' })).toBeInTheDocument()
  })
})

describe('App: accessibility', () => {
  beforeEach(() => signIn(ADMIN))

  it('has a skip link that moves the focus to the main content without changing the route', async () => {
    const { user } = setup({ hash: '#/library' })
    render(App)
    await screen.findByRole('heading', { level: 1, name: 'Library' })
    const skip = screen.getByRole('link', { name: 'Skip to main content' })
    await user.click(skip)
    expect(screen.getByRole('main')).toHaveFocus()
    expect(router.route.name).toBe('library')
    expect(window.location.hash).toBe('#/library')
  })

  it('has the expected landmarks', async () => {
    setup()
    render(App)
    await screen.findByRole('heading', { level: 1, name: 'Dashboard' })
    expect(screen.getByRole('banner')).toBeInTheDocument()
    expect(screen.getByRole('main')).toHaveAttribute('id', 'main')
    expect(screen.getByRole('contentinfo')).toBeInTheDocument()
  })

  it('moves the focus to the main content after navigating to another page', async () => {
    const { user } = setup()
    render(App)
    await screen.findByRole('heading', { level: 1, name: 'Dashboard' })
    await user.click(within(nav()).getByRole('link', { name: 'Upload' }))
    await screen.findByRole('heading', { level: 1, name: 'Upload package' })
    await waitFor(() => expect(screen.getByRole('main')).toHaveFocus())
  })

  it('keeps the focus in the search box while the filters change the address', async () => {
    const { user } = setup({ hash: '#/library' })
    render(App)
    const search = await screen.findByLabelText('Search')
    await user.click(search)
    await user.type(search, 'abc{Enter}')
    await waitFor(() => expect(window.location.hash).toBe('#/library?q=abc'))
    expect(search).toHaveFocus()
  })

  it('announces notifications in a polite live region', async () => {
    setup()
    render(App)
    await screen.findByRole('heading', { level: 1, name: 'Dashboard' })
    const live = document.querySelector('.toasts')
    expect(live).toHaveAttribute('aria-live', 'polite')
    expect(live).toHaveAttribute('role', 'status')
  })
})
