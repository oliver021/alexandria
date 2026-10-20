import { render, screen, waitFor, within } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toasts } from '../lib/toast.svelte.js'
import {
  emptyResponse,
  errorResponse,
  jsonResponse,
  mockFetch,
  resetApp,
  scrolledIntoView,
  signIn,
} from '../test/helpers.js'
import Tokens from './Tokens.svelte'

const SECRET = 'alx_Zk3pQ9vVn2xTqLw8R7aBc1dEfGhIjKlMnOpQrStUvWx'

function token(overrides = {}) {
  return {
    id: 'tok-0001',
    name: 'CI pipeline',
    prefix: 'alx_Zk3pQ9vV',
    scope: 'read',
    created_at: '2026-03-01T10:00:00Z',
    expires_at: null,
    last_used_at: null,
    revoked_at: null,
    ...overrides,
  }
}

function setup(initial = [token()], extra = []) {
  const state = { tokens: [...initial] }
  const http = mockFetch([
    ...extra,
    { method: 'GET', path: '/tokens', respond: () => jsonResponse(state.tokens) },
    {
      method: 'POST',
      path: '/tokens',
      respond: ({ body }) => {
        const data = JSON.parse(body)
        const created = token({
          id: `tok-${state.tokens.length + 1}`,
          name: data.name,
          scope: data.scope,
          prefix: SECRET.slice(0, 12),
          expires_at: data.expires_in_days ? '2026-06-01T00:00:00Z' : null,
        })
        state.tokens = [created, ...state.tokens]
        return jsonResponse({ ...created, token: SECRET }, 201)
      },
    },
    {
      method: 'DELETE',
      path: /^\/tokens\/[^/]+$/,
      respond: ({ path }) => {
        const id = path.split('/').pop()
        state.tokens = state.tokens.map((item) =>
          item.id === id ? { ...item, revoked_at: '2026-03-10T10:00:00Z' } : item,
        )
        return emptyResponse()
      },
    },
  ])
  return { http, state, user: userEvent.setup() }
}

beforeEach(() => {
  resetApp()
  signIn()
})

afterEach(resetApp)

async function create(user, { name = 'Deploy bot', scope, expiry } = {}) {
  await user.type(screen.getByLabelText('Name'), name)
  if (scope) await user.selectOptions(screen.getByLabelText('Scope'), scope)
  if (expiry) await user.selectOptions(screen.getByLabelText('Expires'), expiry)
  await user.click(screen.getByRole('button', { name: 'Create token' }))
}

describe('Tokens: list', () => {
  it('shows the prefix, scope, expiry, last use and status of every token', async () => {
    setup([
      token(),
      token({
        id: 'tok-0002',
        name: 'Release script',
        prefix: 'alx_Mnbvcxz1',
        scope: 'write',
        expires_at: '2099-01-01T00:00:00Z',
        last_used_at: '2026-03-09T10:00:00Z',
      }),
      token({ id: 'tok-0003', name: 'Old key', revoked_at: '2026-03-02T00:00:00Z' }),
      token({ id: 'tok-0004', name: 'Stale key', expires_at: '2020-01-01T00:00:00Z' }),
    ])
    render(Tokens)
    const table = await screen.findByRole('table', { name: 'Your API tokens' })
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows).toHaveLength(4)
    const ci = rows[0]
    expect(ci).toHaveTextContent('CI pipeline')
    expect(ci).toHaveTextContent('alx_Zk3pQ9vV')
    expect(within(ci).getByText('read')).toBeInTheDocument()
    expect(within(ci).getByText('Active')).toBeInTheDocument()
    expect(within(ci).getAllByText('Never')).toHaveLength(2)
    expect(within(rows[1]).getByText('write')).toBeInTheDocument()
    expect(within(rows[2]).getByText('Revoked')).toBeInTheDocument()
    expect(within(rows[3]).getByText('Expired')).toBeInTheDocument()
    expect(within(rows[2]).queryByRole('button', { name: /Revoke/ })).not.toBeInTheDocument()
    expect(within(rows[3]).queryByRole('button', { name: /Revoke/ })).not.toBeInTheDocument()
    expect(within(rows[0]).getByRole('button', { name: /Revoke/ })).toBeInTheDocument()
  })

  it('has an empty state', async () => {
    setup([])
    render(Tokens)
    expect(await screen.findByText('You have no tokens yet')).toBeInTheDocument()
  })

  it('has an error state with a retry button', async () => {
    const user = userEvent.setup()
    let fail = true
    mockFetch([
      {
        method: 'GET',
        path: '/tokens',
        respond: () => (fail ? errorResponse(500, 'INTERNAL_ERROR', 'x') : jsonResponse([token()])),
      },
    ])
    render(Tokens)
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/unexpected error/)
    fail = false
    await user.click(within(alert).getByRole('button', { name: /Try again/ }))
    expect(await screen.findByText('CI pipeline')).toBeInTheDocument()
  })

  it('shows a loading state first', () => {
    setup()
    render(Tokens)
    expect(screen.getByText(/Loading tokens/)).toBeInTheDocument()
  })
})

describe('Tokens: creating a token shows the secret once', () => {
  it('sends the name, scope and expiry', async () => {
    const { http, user } = setup()
    render(Tokens)
    await screen.findByText('CI pipeline')
    await create(user, { name: '  Deploy bot  ', scope: 'write', expiry: '30' })
    await screen.findByLabelText(/Token .Deploy bot./)
    const [call] = http.callsTo('POST', '/tokens')
    expect(JSON.parse(call.body)).toEqual({ name: 'Deploy bot', scope: 'write', expires_in_days: 30 })
  })

  it('moves the focus to the new token so it can be copied right away', async () => {
    const { user } = setup()
    render(Tokens)
    await screen.findByText('CI pipeline')
    await create(user)
    const field = await screen.findByLabelText(/Token .Deploy bot./)
    await waitFor(() => expect(field).toHaveFocus())
  })

  it('shows the clear-text token in a one-time panel with a warning', async () => {
    const { user } = setup()
    render(Tokens)
    await screen.findByText('CI pipeline')
    await create(user)
    const field = await screen.findByLabelText(/Token .Deploy bot./)
    expect(field).toHaveValue(SECRET)
    expect(field).toHaveAttribute('readonly')
    expect(screen.getByText('Copy your new token now')).toBeInTheDocument()
    expect(screen.getByText(/only time it is shown/)).toBeInTheDocument()
  })

  it('copies the token with the copy button', async () => {
    const { user } = setup()
    render(Tokens)
    await screen.findByText('CI pipeline')
    await create(user)
    await screen.findByLabelText(/Token .Deploy bot./)
    await user.click(screen.getByRole('button', { name: 'Copy token' }))
    expect(await navigator.clipboard.readText()).toBe(SECRET)
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument()
  })

  it('forgets the token when it is dismissed', async () => {
    const { user } = setup()
    render(Tokens)
    await screen.findByText('CI pipeline')
    await create(user)
    await screen.findByLabelText(/Token .Deploy bot./)
    await user.click(screen.getByRole('button', { name: 'I have stored it safely' }))
    expect(screen.queryByDisplayValue(SECRET)).not.toBeInTheDocument()
    expect(screen.queryByText('Copy your new token now')).not.toBeInTheDocument()
    expect(document.body.textContent).not.toContain(SECRET)
  })

  it('keeps the secret in memory only', async () => {
    const { user } = setup()
    render(Tokens)
    await screen.findByText('CI pipeline')
    await create(user)
    await screen.findByLabelText(/Token .Deploy bot./)
    const stored = JSON.stringify({ ...window.sessionStorage }) + JSON.stringify({ ...window.localStorage })
    expect(stored).not.toContain(SECRET)
    expect(window.location.href).not.toContain(SECRET)
    expect(document.title).not.toContain(SECRET)
    expect(toasts.items.map((toast) => toast.message).join(' ')).not.toContain(SECRET)
  })

  it('lists the new token by prefix only', async () => {
    const { user } = setup()
    render(Tokens)
    await screen.findByText('CI pipeline')
    await create(user)
    await screen.findByLabelText(/Token .Deploy bot./)
    const table = await screen.findByRole('table')
    expect(await within(table).findByText('Deploy bot')).toBeInTheDocument()
    expect(table).toHaveTextContent(SECRET.slice(0, 12))
    expect(table.textContent).not.toContain(SECRET)
  })

  it('blocks a second token until the first one has been dismissed', async () => {
    const { user } = setup()
    render(Tokens)
    await screen.findByText('CI pipeline')
    await create(user)
    await screen.findByLabelText(/Token .Deploy bot./)
    expect(screen.getByRole('button', { name: 'Create token' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'I have stored it safely' }))
    expect(screen.getByRole('button', { name: 'Create token' })).toBeEnabled()
  })

  it('does not send an expiry when the token never expires', async () => {
    const { http, user } = setup()
    render(Tokens)
    await screen.findByText('CI pipeline')
    await create(user, { expiry: 'never' })
    await screen.findByLabelText(/Token .Deploy bot./)
    expect(JSON.parse(http.callsTo('POST', '/tokens')[0].body)).toEqual({
      name: 'Deploy bot',
      scope: 'read',
    })
  })

  it('accepts a custom number of days', async () => {
    const { http, user } = setup()
    render(Tokens)
    await screen.findByText('CI pipeline')
    await user.selectOptions(screen.getByLabelText('Expires'), 'custom')
    await user.type(screen.getByLabelText('Days until it expires'), '45')
    await user.type(screen.getByLabelText('Name'), 'Custom')
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    await screen.findByLabelText(/Token .Custom./)
    expect(JSON.parse(http.callsTo('POST', '/tokens')[0].body).expires_in_days).toBe(45)
  })

  it('validates the name and the custom days before sending anything', async () => {
    const { http, user } = setup()
    render(Tokens)
    await screen.findByText('CI pipeline')
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    expect(await screen.findByText(/Give the token a name/)).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Expires'), 'custom')
    await user.type(screen.getByLabelText('Name'), 'x')
    await user.type(screen.getByLabelText('Days until it expires'), '99999')
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    expect(await screen.findByText(/between 1 and 3650/)).toBeInTheDocument()
    expect(http.callsTo('POST', '/tokens')).toHaveLength(0)
  })

  it('shows the server message when the limit of tokens is reached', async () => {
    const { user } = setup([token()], [
      {
        method: 'POST',
        path: '/tokens',
        respond: () =>
          errorResponse(409, 'CONFLICT', 'The limit of 20 active API tokens per user has been reached'),
      },
    ])
    render(Tokens)
    await screen.findByText('CI pipeline')
    await create(user)
    expect(await screen.findByText(/limit of 20 active API tokens/)).toBeInTheDocument()
    expect(screen.queryByText('Copy your new token now')).not.toBeInTheDocument()
  })
})

describe('Tokens: bringing a problem into view', () => {
  // The errors sit next to their fields and the button is at the bottom of the form, so on a short
  // window the first error can be above the visible area.
  const CENTERED = { block: 'center', inline: 'nearest' }

  it('moves the focus to the name when it is missing and scrolls it into view', async () => {
    const { user } = setup()
    render(Tokens)
    await screen.findByText('CI pipeline')
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    const name = screen.getByLabelText('Name')
    await waitFor(() => expect(name).toHaveFocus())
    expect(name).toHaveAttribute('aria-invalid', 'true')
    expect(scrolledIntoView()).toEqual([{ element: name, options: CENTERED }])
  })

  it('moves the focus to the number of days when that is the only problem', async () => {
    const { http, user } = setup()
    render(Tokens)
    await screen.findByText('CI pipeline')
    await user.type(screen.getByLabelText('Name'), 'Deploy bot')
    await user.selectOptions(screen.getByLabelText('Expires'), 'custom')
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    const days = screen.getByLabelText('Days until it expires')
    await waitFor(() => expect(days).toHaveFocus())
    expect(scrolledIntoView()).toEqual([{ element: days, options: CENTERED }])
    expect(http.callsTo('POST', '/tokens')).toHaveLength(0)
  })

  it('goes to the name first when the days are wrong too', async () => {
    const { user } = setup()
    render(Tokens)
    await screen.findByText('CI pipeline')
    await user.selectOptions(screen.getByLabelText('Expires'), 'custom')
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    const name = screen.getByLabelText('Name')
    await waitFor(() => expect(name).toHaveFocus())
    expect(scrolledIntoView().map((item) => item.element)).toEqual([name])
  })

  it('moves the focus to the explanation when the registry refuses the token', async () => {
    const { user } = setup([token()], [
      {
        method: 'POST',
        path: '/tokens',
        respond: () =>
          errorResponse(409, 'CONFLICT', 'The limit of 20 active API tokens per user has been reached'),
      },
    ])
    render(Tokens)
    await screen.findByText('CI pipeline')
    await create(user)
    const alert = await screen.findByRole('alert')
    await waitFor(() => expect(alert).toHaveFocus())
    expect(alert).toHaveAttribute('id', 'token-create-error')
    expect(alert).toHaveTextContent(/limit of 20 active API tokens/)
    expect(scrolledIntoView()).toEqual([{ element: alert, options: CENTERED }])
    expect(screen.getByRole('button', { name: 'Create token' })).toBeEnabled()
  })

  it('leaves the focus on the new token, and scrolls nowhere, when the token is created', async () => {
    const { user } = setup()
    render(Tokens)
    await screen.findByText('CI pipeline')
    await create(user)
    const field = await screen.findByLabelText(/Token .Deploy bot./)
    await waitFor(() => expect(field).toHaveFocus())
    expect(scrolledIntoView()).toEqual([])
  })
})

describe('Tokens: revoking', () => {
  it('asks for confirmation and then revokes the token', async () => {
    const { http, user } = setup()
    render(Tokens)
    await screen.findByText('CI pipeline')
    await user.click(screen.getByRole('button', { name: 'Revoke token CI pipeline' }))
    const dialog = await screen.findByRole('dialog', { name: 'Revoke CI pipeline?' })
    expect(dialog).toHaveTextContent(/loses access/)
    expect(http.callsTo('DELETE', /^\/tokens\//)).toHaveLength(0)
    await user.click(within(dialog).getByRole('button', { name: 'Revoke token' }))
    await waitFor(() => expect(http.callsTo('DELETE', /^\/tokens\//)).toHaveLength(1))
    expect(http.callsTo('DELETE', /^\/tokens\//)[0].path).toBe('/tokens/tok-0001')
    expect(await screen.findByText('Revoked')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Revoke token/ })).not.toBeInTheDocument()
    expect(toasts.items.some((item) => /was revoked/.test(item.message))).toBe(true)
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Your tokens' })).toHaveFocus())
  })

  it('does nothing when the confirmation is cancelled', async () => {
    const { http, user } = setup()
    render(Tokens)
    await screen.findByText('CI pipeline')
    await user.click(screen.getByRole('button', { name: 'Revoke token CI pipeline' }))
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(http.callsTo('DELETE', /^\/tokens\//)).toHaveLength(0)
    expect(screen.getByText('Active')).toBeInTheDocument()
  })

  it('keeps the dialog open and explains a failure', async () => {
    const { user } = setup([token()], [
      {
        method: 'DELETE',
        path: /^\/tokens\//,
        respond: () => errorResponse(404, 'NOT_FOUND', 'Token not found'),
      },
    ])
    render(Tokens)
    await screen.findByText('CI pipeline')
    await user.click(screen.getByRole('button', { name: 'Revoke token CI pipeline' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Revoke token' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/does not exist/)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })
})
