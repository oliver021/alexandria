import { render, screen, waitFor, within } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { router } from '../lib/router.svelte.js'
import {
  entrySummary,
  errorResponse,
  jsonResponse,
  mockFetch,
  resetApp,
  signIn,
} from '../test/helpers.js'
import Library from './Library.svelte'

function page(total, items = [entrySummary()], overrides = {}) {
  return { items, total, limit: 20, offset: 0, ...overrides }
}

const TAGS = [
  { name: 'pdf', count: 5 },
  { name: 'forms', count: 3 },
  { name: 'rest', count: 1 },
]
const CATEGORIES = [
  { name: 'documents', count: 4 },
  { name: 'api-design', count: 2 },
]

function setup({ hash = '#/library', listing, extra = [] } = {}) {
  window.history.replaceState(null, '', hash)
  router.stop()
  router.start()
  const http = mockFetch([
    ...extra,
    { method: 'GET', path: '/skills', respond: listing ?? (() => jsonResponse(page(1))) },
    { method: 'GET', path: '/tags', respond: () => jsonResponse(TAGS) },
    { method: 'GET', path: '/categories', respond: () => jsonResponse(CATEGORIES) },
  ])
  return { http, user: userEvent.setup() }
}

const listCalls = (http) => http.calls.filter((call) => call.path === '/skills')
const lastList = (http) => listCalls(http).at(-1)

beforeEach(() => {
  resetApp()
  signIn()
})

afterEach(() => {
  router.stop()
  resetApp()
})

describe('Library: listing', () => {
  it('lists the entries of the first page with their details', async () => {
    const { http } = setup({
      listing: () =>
        jsonResponse(
          page(2, [
            entrySummary({ id: '1', name: 'pdf-forms', display_name: 'PDF Forms' }),
            entrySummary({
              id: '2',
              name: 'http-reference',
              display_name: 'HTTP Reference',
              kind: 'grounding',
              status: 'draft',
              latest_version: null,
              category: null,
              tags: [],
            }),
          ]),
        ),
    })
    render(Library)
    const link = await screen.findByRole('link', { name: 'PDF Forms' })
    expect(link).toHaveAttribute('href', '#/library/pdf-forms')
    expect(screen.getByText('2 entries')).toBeInTheDocument()
    expect(screen.getByText('No published version')).toBeInTheDocument()
    expect(screen.getByText('Draft', { selector: '.badge' })).toBeInTheDocument()
    expect(screen.getByText('Grounding', { selector: '.badge' })).toBeInTheDocument()
    expect(lastList(http).query.get('limit')).toBe('20')
    expect(lastList(http).query.get('offset')).toBe('0')
  })

  it('shows tags and descriptions as plain text', async () => {
    setup({
      listing: () =>
        jsonResponse(
          page(1, [
            entrySummary({
              description: '<img src=x onerror="window.__pwned=1"> hello',
              tags: ['<b>x</b>'],
            }),
          ]),
        ),
    })
    const { container } = render(Library)
    expect(await screen.findByText(/<img src=x onerror=/)).toBeInTheDocument()
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('b')).toBeNull()
    expect(window.__pwned).toBeUndefined()
  })

  it('shows a loading state, then an empty library', async () => {
    setup({ listing: () => jsonResponse(page(0, [])) })
    render(Library)
    expect(screen.getByText(/Loading entries/)).toBeInTheDocument()
    expect(await screen.findByText('The library is empty')).toBeInTheDocument()
    expect(screen.getAllByRole('link', { name: 'Upload package' })).toHaveLength(2)
  })

  it('shows an error with a retry', async () => {
    const user = userEvent.setup()
    let fail = true
    setup({
      listing: () => (fail ? errorResponse(500, 'INTERNAL_ERROR', 'x') : jsonResponse(page(1))),
    })
    render(Library)
    const alert = await screen.findByRole('alert')
    fail = false
    await user.click(within(alert).getByRole('button', { name: /Try again/ }))
    expect(await screen.findByRole('link', { name: 'PDF Forms' })).toBeInTheDocument()
  })
})

describe('Library: filters are kept in the address', () => {
  it('reads every filter from the URL on load', async () => {
    const { http } = setup({
      hash: '#/library?q=pdf&kind=grounding&category=documents&tag=pdf&tag=forms&status=draft&mine=1&sort=updated&page=2',
      listing: () => jsonResponse(page(45, [entrySummary()], { offset: 20 })),
    })
    render(Library)
    await screen.findByRole('link', { name: 'PDF Forms' })
    const query = lastList(http).query
    expect(query.get('q')).toBe('pdf')
    expect(query.get('kind')).toBe('grounding')
    expect(query.get('category')).toBe('documents')
    expect(query.getAll('tag')).toEqual(['pdf', 'forms'])
    expect(query.get('status')).toBe('draft')
    expect(query.get('mine')).toBe('1')
    expect(query.get('sort')).toBe('updated')
    expect(query.get('offset')).toBe('20')
    expect(screen.getByLabelText('Search')).toHaveValue('pdf')
    expect(screen.getByLabelText('Kind')).toHaveValue('grounding')
    expect(screen.getByLabelText('Status')).toHaveValue('draft')
    expect(screen.getByLabelText('Sort by')).toHaveValue('updated')
    expect(screen.getByRole('checkbox', { name: 'Only my entries' })).toBeChecked()
    expect(screen.getByText(/45 entries match your filters/)).toBeInTheDocument()
  })

  it('updates the address and the request when a select changes', async () => {
    const { http, user } = setup()
    render(Library)
    await screen.findByRole('link', { name: 'PDF Forms' })
    await user.selectOptions(screen.getByLabelText('Kind'), 'skill')
    await waitFor(() => expect(lastList(http).query.get('kind')).toBe('skill'))
    expect(window.location.hash).toBe('#/library?kind=skill')
    await user.selectOptions(await screen.findByLabelText('Category'), 'api-design')
    await waitFor(() => expect(lastList(http).query.get('category')).toBe('api-design'))
    expect(window.location.hash).toBe('#/library?kind=skill&category=api-design')
    await user.selectOptions(screen.getByLabelText('Sort by'), 'created')
    await waitFor(() => expect(lastList(http).query.get('sort')).toBe('created'))
    await user.selectOptions(screen.getByLabelText('Status'), 'published')
    await waitFor(() => expect(lastList(http).query.get('status')).toBe('published'))
    await user.click(screen.getByRole('checkbox', { name: 'Only my entries' }))
    await waitFor(() => expect(lastList(http).query.get('mine')).toBe('1'))
    expect(window.location.hash).toContain('mine=1')
  })

  it('does not add history entries while filtering', async () => {
    const { user } = setup()
    render(Library)
    await screen.findByRole('link', { name: 'PDF Forms' })
    const before = window.history.length
    await user.selectOptions(screen.getByLabelText('Kind'), 'skill')
    await user.selectOptions(screen.getByLabelText('Kind'), 'grounding')
    expect(window.history.length).toBe(before)
  })

  it('searches after a short pause while typing, and resets the page', async () => {
    const { http, user } = setup({ hash: '#/library?page=2', listing: () => jsonResponse(page(45)) })
    render(Library)
    await screen.findByRole('link', { name: 'PDF Forms' })
    const callsBefore = listCalls(http).length
    await user.type(screen.getByLabelText('Search'), 'changelog')
    expect(listCalls(http)).toHaveLength(callsBefore)
    await waitFor(() => expect(lastList(http).query.get('q')).toBe('changelog'), { timeout: 2000 })
    expect(window.location.hash).toBe('#/library?q=changelog')
    expect(lastList(http).query.get('offset')).toBe('0')
    expect(listCalls(http).length).toBe(callsBefore + 1)
  })

  it('searches immediately when the search form is submitted', async () => {
    const { http, user } = setup()
    render(Library)
    await screen.findByRole('link', { name: 'PDF Forms' })
    await user.type(screen.getByLabelText('Search'), 'forms{Enter}')
    await waitFor(() => expect(lastList(http).query.get('q')).toBe('forms'))
  })

  it('selects several tags, repeating the parameter, and removes them with the chips', async () => {
    const { http, user } = setup()
    render(Library)
    await screen.findByRole('link', { name: 'PDF Forms' })
    await user.click(screen.getByText('Tags', { selector: 'summary' }))
    await user.click(await screen.findByRole('checkbox', { name: /^pdf/ }))
    await waitFor(() => expect(lastList(http).query.getAll('tag')).toEqual(['pdf']))
    await user.click(screen.getByRole('checkbox', { name: /^forms/ }))
    await waitFor(() => expect(lastList(http).query.getAll('tag')).toEqual(['pdf', 'forms']))
    expect(window.location.hash).toBe('#/library?tag=pdf&tag=forms')
    expect(screen.getByText('Tags (2)', { selector: 'summary' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Remove tag pdf' }))
    await waitFor(() => expect(lastList(http).query.getAll('tag')).toEqual(['forms']))
    expect(screen.getByRole('checkbox', { name: /^pdf/ })).not.toBeChecked()
  })

  it('says so when the tags cannot be loaded', async () => {
    const { user } = setup({
      extra: [{ method: 'GET', path: '/tags', respond: () => errorResponse(500, 'INTERNAL_ERROR', 'x') }],
    })
    render(Library)
    await screen.findByRole('link', { name: 'PDF Forms' })
    await user.click(screen.getByText('Tags', { selector: 'summary' }))
    expect(await screen.findByText('The tags could not be loaded.')).toBeInTheDocument()
  })

  it('keeps the previous page visible when a refresh fails, and offers a retry', async () => {
    let fail = false
    const { user } = setup({
      listing: () => (fail ? errorResponse(500, 'INTERNAL_ERROR', 'x') : jsonResponse(page(45))),
    })
    render(Library)
    await screen.findByRole('link', { name: 'PDF Forms' })
    fail = true
    await user.click(screen.getByRole('button', { name: 'Next' }))
    expect(await screen.findByText('The list could not be refreshed.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'PDF Forms' })).toBeInTheDocument()
    fail = false
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(screen.queryByText('The list could not be refreshed.')).not.toBeInTheDocument())
  })

  it('closes the tag picker with Escape or a click elsewhere', async () => {
    const { user } = setup()
    const { container } = render(Library)
    await screen.findByRole('link', { name: 'PDF Forms' })
    const picker = container.querySelector('details.tagpicker')
    await user.click(screen.getByText('Tags', { selector: 'summary' }))
    expect(picker).toHaveAttribute('open')
    await user.keyboard('{Escape}')
    expect(picker).not.toHaveAttribute('open')
    expect(screen.getByText('Tags', { selector: 'summary' })).toHaveFocus()
    await user.click(screen.getByText('Tags', { selector: 'summary' }))
    expect(picker).toHaveAttribute('open')
    await user.click(screen.getByLabelText('Search'))
    expect(picker).not.toHaveAttribute('open')
  })

  it('filters the list of tags by name', async () => {
    const { user } = setup()
    render(Library)
    await screen.findByRole('link', { name: 'PDF Forms' })
    await user.click(screen.getByText('Tags', { selector: 'summary' }))
    await user.type(await screen.findByLabelText('Find a tag'), 'res')
    expect(screen.getByRole('checkbox', { name: /^rest/ })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: /^pdf/ })).not.toBeInTheDocument()
    await user.clear(screen.getByLabelText('Find a tag'))
    await user.type(screen.getByLabelText('Find a tag'), 'zzz')
    expect(screen.getByText(/No tag matches/)).toBeInTheDocument()
  })

  it('keeps a tag from the address even when it is not in the popular list', async () => {
    const { http } = setup({ hash: '#/library?tag=rare-tag' })
    render(Library)
    await screen.findByRole('link', { name: 'PDF Forms' })
    expect(lastList(http).query.getAll('tag')).toEqual(['rare-tag'])
    expect(screen.getByRole('button', { name: 'Remove tag rare-tag' })).toBeInTheDocument()
  })

  it('only offers relevance sorting while searching', async () => {
    const { user } = setup()
    render(Library)
    await screen.findByRole('link', { name: 'PDF Forms' })
    expect(screen.getByRole('option', { name: 'Best match' })).toBeDisabled()
    expect(screen.getByLabelText('Sort by')).toHaveValue('name')
    await user.type(screen.getByLabelText('Search'), 'x{Enter}')
    await waitFor(() => expect(screen.getByRole('option', { name: 'Best match' })).toBeEnabled())
    expect(screen.getByLabelText('Sort by')).toHaveValue('relevance')
  })

  it('clears every filter at once', async () => {
    const { http, user } = setup({
      hash: '#/library?q=pdf&kind=skill&tag=a',
      listing: ({ query }) =>
        jsonResponse(query.get('kind') ? page(0, []) : page(1)),
    })
    render(Library)
    expect(await screen.findByText('No entries match these filters')).toBeInTheDocument()
    await user.click(screen.getAllByRole('button', { name: 'Clear filters' })[0])
    expect(await screen.findByRole('link', { name: 'PDF Forms' })).toBeInTheDocument()
    expect(window.location.hash).toBe('#/library')
    expect(screen.getByLabelText('Search')).toHaveValue('')
    expect(lastList(http).query.has('kind')).toBe(false)
  })

  it('follows the address when the user navigates back', async () => {
    const { http } = setup({ hash: '#/library?q=one' })
    render(Library)
    await screen.findByRole('link', { name: 'PDF Forms' })
    router.navigate('/library', { query: { q: 'two' }, replace: true })
    await waitFor(() => expect(lastList(http).query.get('q')).toBe('two'))
    expect(screen.getByLabelText('Search')).toHaveValue('two')
  })
})

describe('Library: pagination', () => {
  it('moves between pages and keeps the filters', async () => {
    const { http, user } = setup({
      hash: '#/library?kind=skill',
      listing: ({ query }) => jsonResponse(page(45, [entrySummary()], { offset: Number(query.get('offset')) })),
    })
    render(Library)
    await screen.findByRole('link', { name: 'PDF Forms' })
    expect(screen.getByText(/Showing 1.20 of 45/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Next' }))
    await waitFor(() => expect(lastList(http).query.get('offset')).toBe('20'))
    expect(window.location.hash).toBe('#/library?kind=skill&page=2')
    expect(lastList(http).query.get('kind')).toBe('skill')
    await user.click(screen.getByRole('button', { name: 'Page 3' }))
    await waitFor(() => expect(lastList(http).query.get('offset')).toBe('40'))
    expect(await screen.findByText(/Showing 41.45 of 45/)).toBeInTheDocument()
  })

  it('jumps back to the last page when the address asks for a page that does not exist', async () => {
    const { http } = setup({
      hash: '#/library?page=9',
      listing: ({ query }) =>
        jsonResponse(page(45, Number(query.get('offset')) >= 45 ? [] : [entrySummary()])),
    })
    render(Library)
    await waitFor(() => expect(window.location.hash).toBe('#/library?page=3'))
    await waitFor(() => expect(lastList(http).query.get('offset')).toBe('40'))
  })
})
