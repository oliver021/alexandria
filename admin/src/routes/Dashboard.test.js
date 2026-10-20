import { render, screen, waitFor, within } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  ADMIN,
  PUBLISHER,
  entrySummary,
  errorResponse,
  jsonResponse,
  mockFetch,
  registryInfo,
  resetApp,
  signIn,
} from '../test/helpers.js'
import Dashboard from './Dashboard.svelte'

function totalFor(query) {
  if (query.get('kind') === 'skill') return 2
  if (query.get('kind') === 'grounding') return 1
  if (query.get('status') === 'published') return 2
  if (query.get('status') === 'draft') return 1
  if (query.get('status') === 'unpublished') return 0
  if (query.get('mine')) return 2
  return 3
}

function listing({ query }) {
  if (query.get('sort') === 'updated') {
    return jsonResponse({
      items: [
        entrySummary({ id: 'a', name: 'pdf-forms', display_name: 'PDF Forms' }),
        entrySummary({ id: 'b', name: 'http-reference', display_name: 'HTTP Reference', kind: 'grounding', status: 'draft' }),
      ],
      total: 3,
      limit: 6,
      offset: 0,
    })
  }
  return jsonResponse({ items: [], total: totalFor(query), limit: 1, offset: 0 })
}

function setup(extra = []) {
  return mockFetch([
    ...extra,
    { method: 'GET', path: '/skills', respond: listing },
    { method: 'GET', path: '/info', respond: () => jsonResponse(registryInfo()) },
  ])
}

beforeEach(() => {
  resetApp()
  signIn(ADMIN)
})

afterEach(resetApp)

function tile(label) {
  return screen.getByText(label).closest('a')
}

describe('Dashboard', () => {
  it('counts entries by kind and status with limit=1 queries', async () => {
    const http = setup()
    render(Dashboard)
    await screen.findByText('Entries you can see')
    await screen.findAllByText('3')
    expect(within(tile('Entries you can see')).getByText('3')).toBeInTheDocument()
    expect(within(tile('Skills')).getByText('2')).toBeInTheDocument()
    expect(within(tile('Grounding packages')).getByText('1')).toBeInTheDocument()
    expect(within(tile('Owned by you')).getByText('2')).toBeInTheDocument()

    const counting = http.calls.filter((call) => call.path === '/skills' && call.query.get('limit') === '1')
    const filters = counting.map((call) => ({
      kind: call.query.get('kind'),
      status: call.query.get('status'),
      mine: call.query.get('mine'),
    }))
    expect(filters).toEqual(
      expect.arrayContaining([
        { kind: null, status: null, mine: null },
        { kind: 'skill', status: null, mine: null },
        { kind: 'grounding', status: null, mine: null },
        { kind: null, status: 'published', mine: null },
        { kind: null, status: 'draft', mine: null },
        { kind: null, status: 'unpublished', mine: null },
        { kind: null, status: null, mine: '1' },
      ]),
    )
    expect(counting).toHaveLength(7)
  })

  it('shows the status breakdown with links to the filtered library', async () => {
    setup()
    render(Dashboard)
    const published = await screen.findByRole('link', { name: 'Published' })
    expect(published).toHaveAttribute('href', '#/library?status=published')
    expect(screen.getByRole('link', { name: 'Draft' })).toHaveAttribute('href', '#/library?status=draft')
    expect(screen.getByRole('link', { name: 'Hidden' })).toHaveAttribute('href', '#/library?status=unpublished')
    await waitFor(() => expect(published.closest('li')).toHaveTextContent('2'))
  })

  it('links every tile to the filtered library', async () => {
    setup()
    render(Dashboard)
    await screen.findAllByText('3')
    expect(tile('Skills')).toHaveAttribute('href', '#/library?kind=skill')
    expect(tile('Grounding packages')).toHaveAttribute('href', '#/library?kind=grounding')
    expect(tile('Owned by you')).toHaveAttribute('href', '#/library?mine=1')
    expect(tile('Entries you can see')).toHaveAttribute('href', '#/library')
  })

  it('lists the latest entries', async () => {
    setup()
    render(Dashboard)
    const link = await screen.findByRole('link', { name: 'PDF Forms' })
    expect(link).toHaveAttribute('href', '#/library/pdf-forms')
    expect(screen.getByRole('link', { name: 'HTTP Reference' })).toBeInTheDocument()
    expect(screen.getByText('Draft', { selector: '.badge' })).toBeInTheDocument()
  })

  it('shows the state of the registry from /info', async () => {
    setup()
    render(Dashboard)
    expect(await screen.findByText('Online')).toBeInTheDocument()
    expect(screen.getByText('Alexandria 0.1.0')).toBeInTheDocument()
    expect(screen.getByText('https://registry.example.test/mcp')).toBeInTheDocument()
    expect(screen.getByText(/25 MiB per package, 2,000 files, 100 MiB unpacked/)).toBeInTheDocument()
    expect(screen.getByText('Public')).toBeInTheDocument()
  })

  it('flags a private registry', async () => {
    mockFetch([
      { method: 'GET', path: '/skills', respond: listing },
      { method: 'GET', path: '/info', respond: () => jsonResponse(registryInfo({ public_read: false })) },
    ])
    render(Dashboard)
    expect(await screen.findByText('Private')).toBeInTheDocument()
  })

  it('offers quick access, with user management only for admins', async () => {
    setup()
    render(Dashboard)
    await screen.findByText('Online')
    expect(screen.getByRole('link', { name: 'Upload a package' })).toHaveAttribute('href', '#/upload')
    expect(screen.getByRole('link', { name: 'Create an API token' })).toHaveAttribute('href', '#/tokens')
    expect(screen.getByRole('link', { name: 'Manage users' })).toHaveAttribute('href', '#/users')
    expect(screen.getByRole('link', { name: 'Open the public registry' })).toHaveAttribute('href', '/registry')
  })

  it('hides user management from publishers', async () => {
    resetApp()
    signIn(PUBLISHER)
    setup()
    render(Dashboard)
    await screen.findByText('Online')
    expect(screen.queryByRole('link', { name: 'Manage users' })).not.toBeInTheDocument()
  })

  it('shows a dash for a count that fails without losing the others', async () => {
    setup([{ method: 'GET', path: '/skills', respond: ({ query }) =>
      query.get('status') === 'draft' ? errorResponse(500, 'INTERNAL_ERROR', 'x') : listing({ query }) }])
    render(Dashboard)
    const draft = await screen.findByRole('link', { name: 'Draft' })
    await waitFor(() => expect(within(tile('Skills')).getByText('2')).toBeInTheDocument())
    expect(draft.closest('li')).toHaveTextContent('—')
  })

  it('shows an error with a retry when nothing can be counted', async () => {
    mockFetch([
      { method: 'GET', path: '/skills', respond: () => errorResponse(500, 'INTERNAL_ERROR', 'x') },
      { method: 'GET', path: '/info', respond: () => jsonResponse(registryInfo()) },
    ])
    render(Dashboard)
    expect(await screen.findByText('The totals could not be loaded')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Try again/ }).length).toBeGreaterThan(0)
  })

  it('explains when the registry information cannot be loaded, and retries', async () => {
    let fail = true
    mockFetch([
      { method: 'GET', path: '/skills', respond: listing },
      {
        method: 'GET',
        path: '/info',
        respond: () => (fail ? errorResponse(500, 'INTERNAL_ERROR', 'x') : jsonResponse(registryInfo())),
      },
    ])
    const { user } = { user: userEvent.setup() }
    render(Dashboard)
    const section = (await screen.findByRole('heading', { name: 'Registry' })).closest('section')
    expect(await within(section).findByRole('alert')).toHaveTextContent(/unexpected error/)
    fail = false
    await user.click(within(section).getByRole('button', { name: 'Try again' }))
    expect(await within(section).findByText('Online')).toBeInTheDocument()
  })

  it('has an empty state when there are no entries', async () => {
    mockFetch([
      {
        method: 'GET',
        path: '/skills',
        respond: () => jsonResponse({ items: [], total: 0, limit: 6, offset: 0 }),
      },
      { method: 'GET', path: '/info', respond: () => jsonResponse(registryInfo()) },
    ])
    render(Dashboard)
    expect(await screen.findByText('No entries yet')).toBeInTheDocument()
    expect(screen.getByText(/nothing to break down/)).toBeInTheDocument()
  })
})
