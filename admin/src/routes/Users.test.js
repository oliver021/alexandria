import { render, screen, waitFor, within } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { resetRouteDiscovery } from '../lib/api.js'
import { toasts } from '../lib/toast.svelte.js'
import {
  ADMIN,
  PUBLISHER,
  errorResponse,
  jsonResponse,
  mockFetch,
  resetApp,
  scrolledIntoView,
  signIn,
} from '../test/helpers.js'
import Users from './Users.svelte'

function users() {
  return [
    { ...ADMIN },
    { ...PUBLISHER },
    {
      id: 'user-old-0003',
      username: 'linus',
      email: 'linus@example.test',
      role: 'publisher',
      is_active: false,
      created_at: '2026-01-03T10:00:00Z',
    },
  ]
}

const USER_PATHS = {
  '/api/v1/users': { get: { summary: 'List users' }, post: { summary: 'Create a user' } },
}

const WITH_DEACTIVATION = {
  openapi: '3.1.0',
  paths: {
    ...USER_PATHS,
    '/api/v1/users/{user_id}/deactivate': { post: { summary: 'Deactivate a user' } },
  },
}

const WITHOUT_DEACTIVATION = { openapi: '3.1.0', paths: USER_PATHS }

function documentOf(spec) {
  return { method: 'GET', path: '/openapi.json', respond: () => jsonResponse(spec) }
}

function setup(extra = []) {
  const state = { users: users() }
  const http = mockFetch([
    ...extra,
    documentOf(WITH_DEACTIVATION),
    { method: 'GET', path: '/users', respond: () => jsonResponse(state.users) },
    {
      method: 'POST',
      path: '/users',
      respond: ({ body }) => {
        const data = JSON.parse(body)
        const created = {
          id: 'user-new-0004',
          username: data.username,
          email: data.email,
          role: data.role,
          is_active: true,
          created_at: '2026-03-10T10:00:00Z',
        }
        state.users = [...state.users, created]
        return jsonResponse(created, 201)
      },
    },
    {
      method: 'POST',
      path: /^\/users\/[^/]+\/deactivate$/,
      respond: ({ path }) => {
        const id = path.split('/')[2]
        state.users = state.users.map((item) => (item.id === id ? { ...item, is_active: false } : item))
        return jsonResponse(state.users.find((item) => item.id === id))
      },
    },
  ])
  return { http, state, user: userEvent.setup() }
}

const rowFor = (name) => screen.getByRole('row', { name: new RegExp(name) })

beforeEach(() => {
  resetApp()
  resetRouteDiscovery()
  signIn(ADMIN)
})

afterEach(resetApp)

describe('Users: access', () => {
  it('is only for admins and does not even ask the API otherwise', async () => {
    resetApp()
    signIn(PUBLISHER)
    const { http } = setup()
    render(Users)
    expect(await screen.findByText('Admins only')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Username')).not.toBeInTheDocument()
    expect(http.calls).toHaveLength(0)
  })
})

describe('Users: list', () => {
  it('shows every account with role and status', async () => {
    setup()
    render(Users)
    await screen.findByRole('table', { name: 'User accounts' })
    expect(within(rowFor('ada')).getByText('Admin')).toBeInTheDocument()
    expect(within(rowFor('ada')).getByText('Active')).toBeInTheDocument()
    expect(within(rowFor('grace')).getByText('Publisher')).toBeInTheDocument()
    expect(within(rowFor('linus')).getByText('Inactive')).toBeInTheDocument()
    expect(within(rowFor('grace')).getByText('grace@example.test')).toBeInTheDocument()
  })

  it('has an error state with a retry', async () => {
    const user = userEvent.setup()
    let fail = true
    mockFetch([
      {
        method: 'GET',
        path: '/users',
        respond: () => (fail ? errorResponse(500, 'INTERNAL_ERROR', 'x') : jsonResponse(users())),
      },
    ])
    render(Users)
    const alert = await screen.findByRole('alert')
    fail = false
    await user.click(within(alert).getByRole('button', { name: /Try again/ }))
    expect(await screen.findByRole('table')).toBeInTheDocument()
  })
})

describe('Users: deactivating', () => {
  async function openList() {
    render(Users)
    await screen.findByRole('table')
    await screen.findByRole('button', { name: 'Deactivate grace' })
  }

  it('never offers to deactivate your own account', async () => {
    const { http } = setup()
    render(Users)
    await screen.findByRole('table')
    const own = rowFor('ada')
    expect(await within(own).findByText('This is your account')).toBeInTheDocument()
    expect(within(own).getByText('You')).toBeInTheDocument()
    expect(within(own).queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Deactivate ada/ })).not.toBeInTheDocument()
    expect(http.callsTo('POST', /deactivate/)).toHaveLength(0)
  })

  it('does not offer to deactivate an account that is already inactive', async () => {
    setup()
    await openList()
    expect(within(rowFor('linus')).queryByRole('button')).not.toBeInTheDocument()
  })

  it('asks for confirmation and then deactivates another user', async () => {
    const { http, user } = setup()
    await openList()
    await user.click(screen.getByRole('button', { name: 'Deactivate grace' }))
    const dialog = await screen.findByRole('dialog', { name: 'Deactivate grace?' })
    expect(dialog).toHaveTextContent(/no longer be able to sign in/)
    expect(http.callsTo('POST', /deactivate/)).toHaveLength(0)
    await user.click(within(dialog).getByRole('button', { name: 'Deactivate user' }))
    await waitFor(() => expect(http.callsTo('POST', /deactivate/)).toHaveLength(1))
    expect(http.callsTo('POST', /deactivate/)[0].path).toBe('/users/user-publisher-0002/deactivate')
    await waitFor(() => expect(within(rowFor('grace')).getByText('Inactive')).toBeInTheDocument())
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(toasts.items.some((item) => item.message === 'grace was deactivated.')).toBe(true)
    await waitFor(() => expect(screen.getByRole('heading', { name: 'All users' })).toHaveFocus())
  })

  it('does nothing when the confirmation is cancelled', async () => {
    const { http, user } = setup()
    await openList()
    await user.click(screen.getByRole('button', { name: 'Deactivate grace' }))
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(http.callsTo('POST', /deactivate/)).toHaveLength(0)
    expect(within(rowFor('grace')).getByText('Active')).toBeInTheDocument()
  })

  it('shows the message of the server when it refuses', async () => {
    const { user } = setup([
      {
        method: 'POST',
        path: /deactivate$/,
        respond: () => errorResponse(409, 'CONFLICT', 'An admin cannot deactivate their own account'),
      },
    ])
    await openList()
    await user.click(screen.getByRole('button', { name: 'Deactivate grace' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Deactivate user' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'An admin cannot deactivate their own account',
    )
  })

  it('treats the bare Not Found of a missing route as an unsupported feature', async () => {
    const { user } = setup([
      {
        method: 'POST',
        path: /deactivate$/,
        respond: () => errorResponse(404, 'NOT_FOUND', 'Not Found'),
      },
    ])
    await openList()
    await user.click(screen.getByRole('button', { name: 'Deactivate grace' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Deactivate user' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/does not offer user deactivation/)
  })

  it('still reports a user that really does not exist', async () => {
    const { user } = setup([
      {
        method: 'POST',
        path: /deactivate$/,
        respond: () => errorResponse(404, 'NOT_FOUND', 'User not found'),
      },
    ])
    await openList()
    await user.click(screen.getByRole('button', { name: 'Deactivate grace' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Deactivate user' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/does not exist/)
  })

  it('explains when the registry does not support deactivation', async () => {
    const { user } = setup([
      {
        method: 'POST',
        path: /deactivate$/,
        respond: () => errorResponse(405, 'METHOD_NOT_ALLOWED', 'Method Not Allowed'),
      },
    ])
    await openList()
    await user.click(screen.getByRole('button', { name: 'Deactivate grace' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Deactivate user' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/does not offer user deactivation/)
  })
})

describe('Users: deactivation support', () => {
  const deactivateButtons = () => screen.queryAllByRole('button', { name: /Deactivate/ })

  function expectNothingToDeactivate(http) {
    expect(deactivateButtons()).toHaveLength(0)
    expect(screen.queryByRole('columnheader', { name: 'Actions' })).not.toBeInTheDocument()
    expect(screen.queryByText('This is your account')).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText(/no longer be able to sign in/)).not.toBeInTheDocument()
    expect(screen.queryByText(/A deactivated account cannot sign in/)).not.toBeInTheDocument()
    expect(screen.queryByText(/You cannot deactivate your own account/)).not.toBeInTheDocument()
    expect(http.calls.filter((call) => call.method !== 'GET')).toHaveLength(0)
  }

  it('describes deactivation and shows the actions only when the registry offers it', async () => {
    setup()
    render(Users)
    await screen.findByRole('table')
    expect(await screen.findByText(/A deactivated account cannot sign in/)).toBeInTheDocument()
    expect(screen.getByText(/You cannot deactivate your own account/)).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Actions' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Deactivate grace' })).toBeInTheDocument()
    expect(screen.queryByText(/has no operation to deactivate accounts/)).not.toBeInTheDocument()
  })

  it('offers no deactivation when the registry documents no route for it', async () => {
    const { http } = setup([documentOf(WITHOUT_DEACTIVATION)])
    render(Users)
    await screen.findByRole('table')
    expect(await screen.findByText(/no operation to deactivate accounts/)).toBeInTheDocument()
    expectNothingToDeactivate(http)
    expect(within(rowFor('ada')).getByText('You')).toBeInTheDocument()
    expect(within(rowFor('grace')).getByText('Active')).toBeInTheDocument()
    expect(http.callsTo('GET', '/openapi.json')).toHaveLength(1)
  })

  it.each([
    ['answers with an error', () => errorResponse(500, 'INTERNAL_ERROR', 'x')],
    ['is not there', () => errorResponse(404, 'NOT_FOUND', 'Not Found')],
    ['is not JSON', () => new Response('<html>proxy page</html>', { status: 200 })],
    [
      'cannot be fetched',
      () => {
        throw new TypeError('Failed to fetch')
      },
    ],
  ])('offers no deactivation, and guesses no route, when the API description %s', async (_name, respond) => {
    const { http } = setup([{ method: 'GET', path: '/openapi.json', respond }])
    render(Users)
    await screen.findByRole('table')
    expect(await screen.findByText(/could not read the description of the registry's API/)).toBeInTheDocument()
    expect(screen.getByText(/Reload the page to try again/)).toBeInTheDocument()
    expectNothingToDeactivate(http)
    expect(http.calls.map((call) => call.path)).not.toContain('/users/user-publisher-0002/deactivate')
  })

  it('shows the list without waiting for the API description, and offers nothing meanwhile', async () => {
    let release
    const { http, user } = setup([
      {
        method: 'GET',
        path: '/openapi.json',
        respond: () =>
          new Promise((resolve) => {
            release = () => resolve(jsonResponse(WITH_DEACTIVATION))
          }),
      },
    ])
    render(Users)
    await screen.findByRole('table')
    expect(within(rowFor('grace')).getByText('Active')).toBeInTheDocument()
    expect(http.callsTo('GET', '/openapi.json')).toHaveLength(1)
    expect(deactivateButtons()).toHaveLength(0)
    expect(screen.queryByText(/A deactivated account cannot sign in/)).not.toBeInTheDocument()
    expect(screen.queryByText(/no operation to deactivate accounts/)).not.toBeInTheDocument()
    expect(screen.queryByText(/could not read the description/)).not.toBeInTheDocument()
    release()
    await user.click(await screen.findByRole('button', { name: 'Deactivate grace' }))
    expect(await screen.findByRole('dialog', { name: 'Deactivate grace?' })).toBeInTheDocument()
  })

  it('reads the API description again the next time the page opens after a failure', async () => {
    let readable = false
    const { http } = setup([
      {
        method: 'GET',
        path: '/openapi.json',
        respond: () =>
          readable ? jsonResponse(WITH_DEACTIVATION) : errorResponse(503, 'UNAVAILABLE', 'later'),
      },
    ])
    const first = render(Users)
    await screen.findByText(/could not read the description/)
    expect(deactivateButtons()).toHaveLength(0)
    first.unmount()
    readable = true
    render(Users)
    expect(await screen.findByRole('button', { name: 'Deactivate grace' })).toBeInTheDocument()
    expect(screen.queryByText(/could not read the description/)).not.toBeInTheDocument()
    expect(http.callsTo('GET', '/openapi.json')).toHaveLength(2)
  })

  it('reads the API description once per visit, however much the page does', async () => {
    const { http, user } = setup()
    render(Users)
    await user.click(await screen.findByRole('button', { name: 'Deactivate grace' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Deactivate user' }))
    await waitFor(() => expect(within(rowFor('grace')).getByText('Inactive')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /Refresh/ }))
    await waitFor(() => expect(http.callsTo('GET', '/users')).toHaveLength(3))
    expect(http.callsTo('GET', '/openapi.json')).toHaveLength(1)
  })

  it('does not read the API description for an account that is not an admin', async () => {
    resetApp()
    signIn(PUBLISHER)
    const { http } = setup()
    render(Users)
    await screen.findByText('Admins only')
    expect(http.callsTo('GET', '/openapi.json')).toHaveLength(0)
  })

  it('keeps the lead of the page true when the registry cannot deactivate accounts', async () => {
    setup([documentOf(WITHOUT_DEACTIVATION)])
    render(Users)
    await screen.findByText(/no operation to deactivate accounts/)
    expect(screen.getByText(/Create accounts and see who can sign in/)).toBeInTheDocument()
    expect(screen.queryByText(/control who can sign in/)).not.toBeInTheDocument()
  })
})

describe('Users: creating', () => {
  async function fill(user, { username = 'margaret', email = 'margaret@example.test', password = 'correct horse', role } = {}) {
    if (username) await user.type(screen.getByLabelText('Username'), username)
    if (email) await user.type(screen.getByLabelText('Email'), email)
    if (password) await user.type(screen.getByLabelText('Initial password'), password)
    if (role) await user.selectOptions(screen.getByLabelText('Role'), role)
  }

  it('creates a publisher by default and refreshes the list', async () => {
    const { http, user } = setup()
    render(Users)
    await screen.findByRole('table')
    await fill(user)
    await user.click(screen.getByRole('button', { name: 'Create user' }))
    await waitFor(() => expect(http.callsTo('POST', '/users')).toHaveLength(1))
    expect(JSON.parse(http.callsTo('POST', '/users')[0].body)).toEqual({
      username: 'margaret',
      email: 'margaret@example.test',
      password: 'correct horse',
      role: 'publisher',
    })
    expect(await screen.findByRole('row', { name: /margaret/ })).toBeInTheDocument()
    expect(screen.getByLabelText('Username')).toHaveValue('')
    expect(screen.getByLabelText('Initial password')).toHaveValue('')
    expect(toasts.items.some((item) => item.message === 'User margaret was created.')).toBe(true)
  })

  it('creates an admin when the role is chosen', async () => {
    const { http, user } = setup()
    render(Users)
    await screen.findByRole('table')
    await fill(user, { role: 'admin' })
    await user.click(screen.getByRole('button', { name: 'Create user' }))
    await waitFor(() => expect(http.callsTo('POST', '/users')).toHaveLength(1))
    expect(JSON.parse(http.callsTo('POST', '/users')[0].body).role).toBe('admin')
  })

  it('validates the fields before sending anything', async () => {
    const { http, user } = setup()
    render(Users)
    await screen.findByRole('table')
    await user.click(screen.getByRole('button', { name: 'Create user' }))
    expect(await screen.findByText('Enter a username.')).toBeInTheDocument()
    expect(screen.getByText('Enter a valid email address.')).toBeInTheDocument()
    expect(screen.getByText(/Use between 8 and 1024 characters/)).toBeInTheDocument()
    await fill(user, { password: 'short' })
    await user.click(screen.getByRole('button', { name: 'Create user' }))
    expect(screen.getByText(/Use between 8 and 1024 characters/)).toBeInTheDocument()
    expect(http.callsTo('POST', '/users')).toHaveLength(0)
  })

  describe('bringing a problem into view', () => {
    // The errors sit next to their fields and the button is at the bottom of the form, so on a short
    // window the first error can be above the visible area.
    const CENTERED = { block: 'center', inline: 'nearest' }

    it('moves the focus to the first field with a problem and scrolls it into view', async () => {
      const { user } = setup()
      render(Users)
      await screen.findByRole('table')
      await user.click(screen.getByRole('button', { name: 'Create user' }))
      const username = screen.getByLabelText('Username')
      await waitFor(() => expect(username).toHaveFocus())
      expect(username).toHaveAttribute('aria-invalid', 'true')
      expect(scrolledIntoView()).toEqual([{ element: username, options: CENTERED }])
    })

    it('goes on to the next field with a problem each time the form is sent again', async () => {
      const { user } = setup()
      render(Users)
      await screen.findByRole('table')
      await user.type(screen.getByLabelText('Username'), 'margaret')
      await user.click(screen.getByRole('button', { name: 'Create user' }))
      const email = screen.getByLabelText('Email')
      await waitFor(() => expect(email).toHaveFocus())

      await user.type(email, 'margaret@example.test')
      await user.click(screen.getByRole('button', { name: 'Create user' }))
      const password = screen.getByLabelText('Initial password')
      await waitFor(() => expect(password).toHaveFocus())
      expect(scrolledIntoView().map((item) => item.element)).toEqual([
        screen.getByLabelText('Email'),
        password,
      ])
    })

    it('sends nothing while a field still has a problem', async () => {
      const { http, user } = setup()
      render(Users)
      await screen.findByRole('table')
      await user.type(screen.getByLabelText('Username'), 'margaret')
      await user.type(screen.getByLabelText('Email'), 'margaret@example.test')
      await user.type(screen.getByLabelText('Initial password'), 'short')
      await user.click(screen.getByRole('button', { name: 'Create user' }))
      await waitFor(() => expect(screen.getByLabelText('Initial password')).toHaveFocus())
      expect(http.callsTo('POST', '/users')).toHaveLength(0)
    })

    it('moves the focus to the explanation when the registry refuses the user', async () => {
      const { user } = setup([
        {
          method: 'POST',
          path: '/users',
          respond: () => errorResponse(409, 'CONFLICT', "Username 'margaret' already exists"),
        },
      ])
      render(Users)
      await screen.findByRole('table')
      await fill(user)
      await user.click(screen.getByRole('button', { name: 'Create user' }))
      const alert = await screen.findByRole('alert')
      await waitFor(() => expect(alert).toHaveFocus())
      expect(alert).toHaveAttribute('id', 'user-create-error')
      expect(alert).toHaveTextContent("Username 'margaret' already exists")
      expect(scrolledIntoView()).toEqual([{ element: alert, options: CENTERED }])
      expect(screen.getByRole('button', { name: 'Create user' })).toBeEnabled()
    })

    it('does not scroll anywhere when the user is created', async () => {
      const { http, user } = setup()
      render(Users)
      await screen.findByRole('table')
      await fill(user)
      await user.click(screen.getByRole('button', { name: 'Create user' }))
      await waitFor(() => expect(http.callsTo('POST', '/users')).toHaveLength(1))
      await screen.findByRole('row', { name: /margaret/ })
      expect(scrolledIntoView()).toEqual([])
    })
  })

  it('shows and hides the password on request', async () => {
    const { user } = setup()
    render(Users)
    await screen.findByRole('table')
    const field = screen.getByLabelText('Initial password')
    expect(field).toHaveAttribute('type', 'password')
    await user.click(screen.getByRole('button', { name: 'Show password' }))
    expect(field).toHaveAttribute('type', 'text')
    await user.click(screen.getByRole('button', { name: 'Hide password' }))
    expect(field).toHaveAttribute('type', 'password')
  })

  it('reports a duplicate user', async () => {
    const { user } = setup([
      {
        method: 'POST',
        path: '/users',
        respond: () => errorResponse(409, 'CONFLICT', "Username 'margaret' already exists"),
      },
    ])
    render(Users)
    await screen.findByRole('table')
    await fill(user)
    await user.click(screen.getByRole('button', { name: 'Create user' }))
    expect(await screen.findByText("Username 'margaret' already exists")).toBeInTheDocument()
    expect(screen.getByLabelText('Username')).toHaveValue('margaret')
  })
})
