import { render, screen, waitFor, within } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { router } from '../lib/router.svelte.js'
import { toasts } from '../lib/toast.svelte.js'
import {
  ADMIN,
  PUBLISHER,
  binaryResponse,
  emptyResponse,
  entryDetail,
  errorResponse,
  jsonResponse,
  mockFetch,
  registryInfo,
  resetApp,
  signIn,
  versionSummary,
} from '../test/helpers.js'
import Entry from './Entry.svelte'

const BINARY = [1, 2, 3, 4]
const BINARY_SHA = Array.from(
  new Uint8Array(await crypto.subtle.digest('SHA-256', new Uint8Array(BINARY))),
  (byte) => byte.toString(16).padStart(2, '0'),
).join('')

const VERSIONS = [
  versionSummary({ id: 'v2', version: '1.2.0', status: 'published' }),
  versionSummary({ id: 'v1', version: '1.1.0', status: 'published' }),
  versionSummary({ id: 'v0', version: '1.0.0-rc.1', status: 'draft', published_at: null }),
]

function manifest(version = '1.2.0') {
  return {
    name: 'pdf-forms',
    version,
    files: [
      { path: 'SKILL.md', size: 120, sha256: 'a'.repeat(64), media_type: 'text/markdown', text: true },
      { path: 'references/api notes.md', size: 40, sha256: 'b'.repeat(64), media_type: 'text/markdown', text: true },
      { path: 'data/forms.sqlite', size: 4096, sha256: BINARY_SHA, media_type: 'application/octet-stream', text: false },
      { path: 'empty.txt', size: 0, sha256: 'd'.repeat(64), media_type: 'text/plain', text: true },
    ],
  }
}

function fileContent(path, content, overrides = {}) {
  return {
    path,
    size: content.length,
    sha256: 'a'.repeat(64),
    media_type: 'text/markdown',
    text: true,
    content,
    truncated: false,
    encoding: 'utf-8',
    ...overrides,
  }
}

function setup({
  hash = '#/library/pdf-forms',
  entry = entryDetail({}, VERSIONS),
  metadata = { author: 'Ada', stars: 5, flags: { beta: true } },
  handlers = [],
  info = registryInfo(),
} = {}) {
  window.history.replaceState(null, '', hash)
  router.stop()
  router.start()
  const state = { entry }
  const http = mockFetch([
    ...handlers,
    { method: 'GET', path: '/skills/pdf-forms', respond: () => jsonResponse(state.entry) },
    {
      method: 'GET',
      path: /^\/skills\/pdf-forms\/versions\/[^/]+$/,
      respond: ({ path }) => {
        const version = decodeURIComponent(path.split('/').pop())
        return jsonResponse({
          ...versionSummary({ version }),
          metadata,
          download_url: `https://registry.example.test/api/v1/skills/pdf-forms/versions/${version}/download`,
        })
      },
    },
    { method: 'GET', path: '/info', respond: () => jsonResponse(info) },
    { method: 'GET', path: '/users', respond: () => jsonResponse([ADMIN, PUBLISHER]) },
    {
      method: 'GET',
      path: /^\/skills\/pdf-forms\/versions\/[^/]+\/files$/,
      respond: ({ path }) => jsonResponse(manifest(path.split('/')[4])),
    },
  ])
  return { http, state, user: userEvent.setup() }
}

beforeEach(() => {
  resetApp()
  signIn(ADMIN)
})

afterEach(() => {
  router.stop()
  resetApp()
})

describe('Entry: loading', () => {
  it('shows a loading state, then the entry', async () => {
    setup()
    render(Entry)
    expect(screen.getByText(/Loading entry/)).toBeInTheDocument()
    expect(await screen.findByRole('heading', { level: 1, name: 'PDF Forms' })).toBeInTheDocument()
    expect(screen.getByText('pdf-forms', { selector: 'p' })).toBeInTheDocument()
  })

  it('explains a missing or invisible entry', async () => {
    setup({
      handlers: [
        { method: 'GET', path: '/skills/pdf-forms', respond: () => errorResponse(404, 'NOT_FOUND', 'Skill not found') },
      ],
    })
    render(Entry)
    expect(await screen.findByRole('heading', { level: 1, name: 'Entry not found' })).toBeInTheDocument()
    expect(screen.getByText(/does not exist, or you are not allowed to see it/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to the library' })).toHaveAttribute('href', '#/library')
  })

  it('shows other errors with a retry', async () => {
    const user = userEvent.setup()
    let fail = true
    setup({
      handlers: [
        {
          method: 'GET',
          path: '/skills/pdf-forms',
          respond: () => (fail ? errorResponse(500, 'INTERNAL_ERROR', 'x') : jsonResponse(entryDetail({}, VERSIONS))),
        },
      ],
    })
    render(Entry)
    const alert = await screen.findByRole('alert')
    fail = false
    await user.click(within(alert).getByRole('button', { name: /Try again/ }))
    expect(await screen.findByRole('heading', { level: 1, name: 'PDF Forms' })).toBeInTheDocument()
  })

  it('encodes the name from the address when it asks for the entry', async () => {
    const http = setup({
      hash: '#/library/pdf%20forms',
      handlers: [{ method: 'GET', path: '/skills/pdf%20forms', respond: () => errorResponse(404, 'NOT_FOUND', 'x') }],
    }).http
    render(Entry)
    await screen.findByText('Entry not found')
    expect(http.calls.find((call) => call.path.startsWith('/skills')).url.pathname).toBe(
      '/api/v1/skills/pdf%20forms',
    )
  })
})

describe('Entry: summary', () => {
  it('shows kind, status, category, tags, version and owner', async () => {
    setup()
    render(Entry)
    await screen.findByRole('heading', { level: 1, name: 'PDF Forms' })
    const details = screen.getByRole('region', { name: 'Details' })
    expect(within(details).getByText('Skill', { selector: '.badge' })).toBeInTheDocument()
    expect(within(details).getByText('Published', { selector: '.badge' })).toBeInTheDocument()
    expect(within(details).getByRole('link', { name: 'documents' })).toHaveAttribute('href', '#/library?category=documents')
    expect(within(details).getByRole('link', { name: 'pdf' })).toHaveAttribute('href', '#/library?tag=pdf')
    expect(within(details).getByText('1.2.0')).toBeInTheDocument()
    expect(within(details).getByText('ada (you)')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Description' })).toHaveTextContent('Fill and read PDF forms.')
  })

  it('shows the metadata of the version as text', async () => {
    setup()
    const { container } = render(Entry)
    const metadata = await screen.findByRole('region', { name: 'Metadata' })
    expect(await within(metadata).findByText('author')).toBeInTheDocument()
    expect(within(metadata).getByText('Ada')).toBeInTheDocument()
    expect(within(metadata).getByText('5')).toBeInTheDocument()
    expect(metadata.querySelector('pre')?.textContent).toContain('"beta": true')
    expect(container.querySelector('script')).toBeNull()
  })

  it('does not interpret metadata as markup', async () => {
    setup({ metadata: { homepage: '<img src=x onerror="window.__pwned=1">' } })
    const { container } = render(Entry)
    expect(await screen.findByText(/<img src=x onerror=/)).toBeInTheDocument()
    expect(container.querySelector('img')).toBeNull()
    expect(window.__pwned).toBeUndefined()
  })

  it('truncates huge metadata values', async () => {
    setup({ metadata: { notes: 'x'.repeat(5000) } })
    render(Entry)
    const metadata = await screen.findByRole('region', { name: 'Metadata' })
    await within(metadata).findByText('notes')
    expect(metadata.textContent.length).toBeLessThan(2500)
  })

  it('explains an entry without extra metadata', async () => {
    setup({ metadata: {} })
    render(Entry)
    expect(await screen.findByText(/declares no fields beyond the standard ones/)).toBeInTheDocument()
  })

  it('resolves the owner for admins and falls back to a short id for publishers', async () => {
    setup({ entry: entryDetail({ owner_id: PUBLISHER.id }, VERSIONS) })
    render(Entry)
    expect(await screen.findByText('grace')).toBeInTheDocument()
  })

  it('does not ask for the user list when the viewer is not an admin', async () => {
    resetApp()
    signIn(PUBLISHER)
    const { http } = setup({ entry: entryDetail({ owner_id: ADMIN.id }, VERSIONS) })
    render(Entry)
    expect(await screen.findByText(`User ${ADMIN.id.slice(0, 8)}`)).toBeInTheDocument()
    expect(http.callsTo('GET', '/users')).toHaveLength(0)
  })
})

describe('Entry: refreshing', () => {
  it('keeps showing the entry when a refresh fails and lets the user retry', async () => {
    const { http, state, user } = setup({ entry: entryDetail({ status: 'unpublished' }, VERSIONS) })
    http.on('POST', '/skills/pdf-forms/restore', () => jsonResponse(entryDetail({}, VERSIONS)))
    render(Entry)
    await user.click(await screen.findByRole('button', { name: 'Restore entry' }))
    const dialog = await screen.findByRole('dialog')
    http.on('GET', '/skills/pdf-forms', () => errorResponse(500, 'INTERNAL_ERROR', 'x'))
    await user.click(within(dialog).getByRole('button', { name: 'Restore entry' }))
    expect(await screen.findByText('The entry could not be refreshed.')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: 'PDF Forms' })).toBeInTheDocument()
    state.entry = entryDetail({}, VERSIONS)
    http.on('GET', '/skills/pdf-forms', () => jsonResponse(state.entry))
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(screen.queryByText('The entry could not be refreshed.')).not.toBeInTheDocument())
  })
})

describe('Entry: tabs', () => {
  it('keeps the selected tab in the address', async () => {
    const { user } = setup()
    render(Entry)
    await screen.findByRole('heading', { level: 1, name: 'PDF Forms' })
    expect(screen.getByRole('tab', { name: 'Summary' })).toHaveAttribute('aria-selected', 'true')
    await user.click(screen.getByRole('tab', { name: 'Versions' }))
    expect(window.location.hash).toBe('#/library/pdf-forms?tab=versions')
    expect(screen.getByRole('table', { name: /Versions of PDF Forms/ })).toBeInTheDocument()
    const panel = screen.getByRole('tabpanel')
    expect(panel).toHaveAttribute('aria-labelledby', 'entry-tab-versions')
    await user.click(screen.getByRole('tab', { name: 'Summary' }))
    expect(window.location.hash).toBe('#/library/pdf-forms')
  })

  it('opens on the tab named in the address', async () => {
    setup({ hash: '#/library/pdf-forms?tab=versions' })
    render(Entry)
    expect(await screen.findByRole('table', { name: /Versions of PDF Forms/ })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Versions' })).toHaveAttribute('aria-selected', 'true')
  })

  it('lists versions newest first by semantic version, whatever order the server used', async () => {
    const shuffled = [
      versionSummary({ id: 'a', version: '1.2.0' }),
      versionSummary({ id: 'b', version: '1.10.0' }),
      versionSummary({ id: 'c', version: '1.0.0-rc.1', status: 'draft' }),
      versionSummary({ id: 'd', version: '2.0.0', status: 'draft' }),
      versionSummary({ id: 'e', version: '1.9.0' }),
    ]
    setup({
      hash: '#/library/pdf-forms?tab=versions',
      entry: entryDetail({ latest_version: '1.10.0' }, shuffled),
    })
    render(Entry)
    const table = await screen.findByRole('table', { name: /Versions of PDF Forms/ })
    const order = within(table)
      .getAllByRole('rowheader')
      .map((cell) => cell.textContent.trim().split(/\s+/)[0])
    expect(order).toEqual(['2.0.0', '1.10.0', '1.9.0', '1.2.0', '1.0.0-rc.1'])
  })

  it('ignores an unknown tab', async () => {
    setup({ hash: '#/library/pdf-forms?tab=bogus' })
    render(Entry)
    await screen.findByRole('heading', { level: 1, name: 'PDF Forms' })
    expect(screen.getByRole('tab', { name: 'Summary' })).toHaveAttribute('aria-selected', 'true')
  })

  it('works from the keyboard', async () => {
    const { user } = setup()
    render(Entry)
    await screen.findByRole('heading', { level: 1, name: 'PDF Forms' })
    screen.getByRole('tab', { name: 'Summary' }).focus()
    await user.keyboard('{ArrowRight}')
    expect(window.location.hash).toBe('#/library/pdf-forms?tab=versions')
    expect(screen.getByRole('tab', { name: 'Versions' })).toHaveFocus()
  })
})

describe('Entry: hiding and restoring', () => {
  it('hides the entry after a confirmation', async () => {
    const { http, state, user } = setup()
    render(Entry)
    await user.click(await screen.findByRole('button', { name: 'Hide entry' }))
    const dialog = await screen.findByRole('dialog', { name: 'Hide PDF Forms?' })
    expect(http.callsTo('DELETE', '/skills/pdf-forms')).toHaveLength(0)
    http.on('DELETE', '/skills/pdf-forms', () => {
      state.entry = entryDetail({ status: 'unpublished' }, VERSIONS)
      return emptyResponse()
    })
    await user.click(within(dialog).getByRole('button', { name: 'Hide entry' }))
    await waitFor(() => expect(http.callsTo('DELETE', '/skills/pdf-forms')).toHaveLength(1))
    expect(await screen.findByRole('button', { name: 'Restore entry' })).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(toasts.items.some((item) => item.message === 'PDF Forms is now hidden.')).toBe(true)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Restore entry' })).toHaveFocus())
  })

  it('does nothing if the confirmation is cancelled', async () => {
    const { http, user } = setup()
    render(Entry)
    await user.click(await screen.findByRole('button', { name: 'Hide entry' }))
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(http.callsTo('DELETE', '/skills/pdf-forms')).toHaveLength(0)
    expect(screen.getByRole('button', { name: 'Hide entry' })).toBeInTheDocument()
  })

  it('restores a hidden entry after a confirmation', async () => {
    const { http, state, user } = setup({ entry: entryDetail({ status: 'unpublished' }, VERSIONS) })
    http.on('POST', '/skills/pdf-forms/restore', () => {
      state.entry = entryDetail({}, VERSIONS)
      return jsonResponse(state.entry)
    })
    render(Entry)
    await user.click(await screen.findByRole('button', { name: 'Restore entry' }))
    const dialog = await screen.findByRole('dialog', { name: 'Restore PDF Forms?' })
    await user.click(within(dialog).getByRole('button', { name: 'Restore entry' }))
    await waitFor(() => expect(http.callsTo('POST', '/skills/pdf-forms/restore')).toHaveLength(1))
    expect(await screen.findByRole('button', { name: 'Hide entry' })).toBeInTheDocument()
  })

  it('explains a refusal inside the dialog', async () => {
    const { user } = setup({
      handlers: [
        {
          method: 'DELETE',
          path: '/skills/pdf-forms',
          respond: () => errorResponse(403, 'FORBIDDEN', 'Only the owner of this entry or an admin can change it'),
        },
      ],
    })
    render(Entry)
    await user.click(await screen.findByRole('button', { name: 'Hide entry' }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Hide entry' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'Only the owner of this entry or an admin can change it',
    )
  })

  it('does not offer visibility changes to people who cannot manage the entry', async () => {
    resetApp()
    signIn(PUBLISHER)
    setup({ entry: entryDetail({ owner_id: ADMIN.id }, VERSIONS) })
    render(Entry)
    await screen.findByRole('heading', { level: 1, name: 'PDF Forms' })
    expect(screen.queryByRole('button', { name: 'Hide entry' })).not.toBeInTheDocument()
    expect(screen.getByText(/Only the owner of this entry and admins can change it/)).toBeInTheDocument()
  })

  it('lets the owner manage a publisher entry', async () => {
    resetApp()
    signIn(PUBLISHER)
    setup({ entry: entryDetail({ owner_id: PUBLISHER.id }, VERSIONS) })
    render(Entry)
    expect(await screen.findByRole('button', { name: 'Hide entry' })).toBeInTheDocument()
  })
})

describe('Entry: files', () => {
  it('shows the manifest as a tree and loads text files as plain text', async () => {
    const { http, user } = setup({
      hash: '#/library/pdf-forms?tab=files',
      handlers: [
        {
          method: 'GET',
          path: '/skills/pdf-forms/versions/1.2.0/files/SKILL.md',
          respond: () => jsonResponse(fileContent('SKILL.md', '# Title\n\nBody text')),
        },
      ],
    })
    render(Entry)
    const tree = await screen.findByRole('tree', { name: 'Files of version 1.2.0' })
    expect(within(tree).getAllByRole('treeitem').map((item) => item.dataset.path)).toEqual([
      'data',
      'data/forms.sqlite',
      'references',
      'references/api notes.md',
      'empty.txt',
      'SKILL.md',
    ])
    expect(http.callsTo('GET', /\/files\//)).toHaveLength(0)
    await user.click(screen.getByRole('treeitem', { name: /SKILL\.md/ }))
    const viewer = await screen.findByRole('region', { name: 'SKILL.md' })
    expect(await within(viewer).findByText(/Body text/)).toBeInTheDocument()
    expect(viewer.querySelector('pre')?.textContent).toBe('# Title\n\nBody text')
    expect(window.location.hash).toBe('#/library/pdf-forms?tab=files&file=SKILL.md')
    expect(within(viewer).getByText('text/markdown')).toBeInTheDocument()
    expect(within(viewer).getByText('a'.repeat(64))).toBeInTheDocument()
  })

  it('never renders the content of a file as HTML', async () => {
    setup({
      hash: '#/library/pdf-forms?tab=files&file=SKILL.md',
      handlers: [
        {
          method: 'GET',
          path: '/skills/pdf-forms/versions/1.2.0/files/SKILL.md',
          respond: () =>
            jsonResponse(fileContent('SKILL.md', '<script>window.__pwned=1</script><img src=x onerror="window.__pwned=2"><b>bold</b>')),
        },
      ],
    })
    const { container } = render(Entry)
    const viewer = await screen.findByRole('region', { name: 'SKILL.md' })
    expect(await within(viewer).findByText(/<script>window\.__pwned=1<\/script>/)).toBeInTheDocument()
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
    expect(viewer.querySelector('b')).toBeNull()
    expect(window.__pwned).toBeUndefined()
  })

  it('encodes nested paths when it reads a file', async () => {
    const { http } = setup({
      hash: '#/library/pdf-forms?tab=files&file=references%2Fapi%20notes.md',
      handlers: [
        {
          method: 'GET',
          path: /files\/references/,
          respond: () => jsonResponse(fileContent('references/api notes.md', 'notes')),
        },
      ],
    })
    render(Entry)
    await screen.findByRole('region', { name: 'references/api notes.md' })
    await waitFor(() => expect(http.callsTo('GET', /files\/references/)).toHaveLength(1))
    expect(http.callsTo('GET', /files\/references/)[0].url.pathname).toBe(
      '/api/v1/skills/pdf-forms/versions/1.2.0/files/references/api%20notes.md',
    )
  })

  it('warns when the preview is truncated', async () => {
    setup({
      hash: '#/library/pdf-forms?tab=files&file=SKILL.md',
      handlers: [
        {
          method: 'GET',
          path: /files\/SKILL\.md$/,
          respond: () => jsonResponse(fileContent('SKILL.md', 'partial', { truncated: true })),
        },
      ],
    })
    render(Entry)
    const alert = await screen.findByText(/Only the first part of this file is shown/)
    await waitFor(() => expect(alert).toHaveTextContent(/at most 256 KiB/))
    expect(alert).toHaveTextContent(/Download the file to read all of it/)
  })

  it('shows binary files as a card, without fetching their content', async () => {
    const { http } = setup({ hash: '#/library/pdf-forms?tab=files&file=data%2Fforms.sqlite' })
    render(Entry)
    const viewer = await screen.findByRole('region', { name: 'data/forms.sqlite' })
    expect(within(viewer).getByText(/This is a binary file/)).toBeInTheDocument()
    expect(viewer.querySelector('pre')).toBeNull()
    expect(within(viewer).getByRole('button', { name: 'Download file' })).toBeInTheDocument()
    expect(within(viewer).getByText('4 KiB')).toBeInTheDocument()
    expect(within(viewer).getByText(BINARY_SHA)).toBeInTheDocument()
    expect(http.callsTo('GET', /\/files\//)).toHaveLength(0)
  })

  describe('downloading a file', () => {
    let saved

    beforeEach(() => {
      saved = []
      URL.createObjectURL = vi.fn(() => 'blob:x')
      URL.revokeObjectURL = vi.fn()
      vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function click() {
        saved.push(this.download)
      })
    })

    it('goes through fetch, keeps the token in the header and saves the file', async () => {
      const { http, user } = setup({
        hash: '#/library/pdf-forms?tab=files&file=data%2Fforms.sqlite',
        handlers: [{ method: 'GET', path: /\/raw\//, respond: () => binaryResponse(BINARY) }],
      })
      render(Entry)
      const viewer = await screen.findByRole('region', { name: 'data/forms.sqlite' })
      await user.click(within(viewer).getByRole('button', { name: 'Download file' }))
      await waitFor(() => expect(saved).toEqual(['forms.sqlite']))
      const [call] = http.callsTo('GET', /\/raw\//)
      expect(call.url.pathname).toBe('/api/v1/skills/pdf-forms/versions/1.2.0/raw/data/forms.sqlite')
      expect(call.url.href).not.toContain('jwt-session-token')
      expect(Object.values(call.headers)).toContain('Bearer jwt-session-token')
    })

    it('refuses to save a file that does not match its checksum', async () => {
      const { user } = setup({
        hash: '#/library/pdf-forms?tab=files&file=data%2Fforms.sqlite',
        handlers: [{ method: 'GET', path: /\/raw\//, respond: () => binaryResponse([9, 9, 9]) }],
      })
      render(Entry)
      const viewer = await screen.findByRole('region', { name: 'data/forms.sqlite' })
      await user.click(within(viewer).getByRole('button', { name: 'Download file' }))
      await waitFor(() => expect(toasts.items.some((item) => item.tone === 'error')).toBe(true))
      expect(toasts.items.find((item) => item.tone === 'error').message).toMatch(/does not match/)
      expect(saved).toEqual([])
    })
  })

  it('says so when a file is empty', async () => {
    setup({
      hash: '#/library/pdf-forms?tab=files&file=empty.txt',
      handlers: [
        { method: 'GET', path: /files\/empty\.txt$/, respond: () => jsonResponse(fileContent('empty.txt', '')) },
      ],
    })
    render(Entry)
    expect(await screen.findByText('This file is empty.')).toBeInTheDocument()
  })

  it('says so when the file of the address is not in the version', async () => {
    setup({ hash: '#/library/pdf-forms?tab=files&file=nope.md' })
    render(Entry)
    expect(await screen.findByText('That file is not in this version')).toBeInTheDocument()
  })

  it('reports a failure to read a file', async () => {
    setup({
      hash: '#/library/pdf-forms?tab=files&file=SKILL.md',
      handlers: [
        { method: 'GET', path: /files\/SKILL\.md$/, respond: () => errorResponse(500, 'STORAGE_ERROR', 'x') },
      ],
    })
    render(Entry)
    expect(await screen.findByText(/stored data of this version is missing or damaged/)).toBeInTheDocument()
  })

  it('switches the version and keeps it in the address', async () => {
    const { http, user } = setup({ hash: '#/library/pdf-forms?tab=files' })
    render(Entry)
    await screen.findByRole('tree', { name: 'Files of version 1.2.0' })
    await user.selectOptions(screen.getByLabelText('Version'), '1.1.0')
    expect(await screen.findByRole('tree', { name: 'Files of version 1.1.0' })).toBeInTheDocument()
    expect(window.location.hash).toBe('#/library/pdf-forms?tab=files&version=1.1.0')
    expect(http.callsTo('GET', /versions\/1\.1\.0\/files$/)).toHaveLength(1)
  })

  it('marks the latest version and the status of every option', async () => {
    setup({ hash: '#/library/pdf-forms?tab=files' })
    render(Entry)
    await screen.findByRole('tree')
    const options = within(screen.getByLabelText('Version')).getAllByRole('option')
    expect(options.map((option) => option.textContent.replace(/\s+/g, ' ').trim())).toEqual([
      '1.2.0 (latest) · Published',
      '1.1.0 · Published',
      '1.0.0-rc.1 · Draft',
    ])
  })

  it('encodes a prerelease version in the file requests', async () => {
    const { http } = setup({ hash: '#/library/pdf-forms?tab=files&version=1.0.0-rc.1' })
    render(Entry)
    await screen.findByRole('tree', { name: 'Files of version 1.0.0-rc.1' })
    expect(http.callsTo('GET', /files$/)[0].url.pathname).toBe(
      '/api/v1/skills/pdf-forms/versions/1.0.0-rc.1/files',
    )
  })
})

describe('Entry: use', () => {
  it('shows ready-to-copy snippets built from the registry information', async () => {
    setup({ hash: '#/library/pdf-forms?tab=use' })
    render(Entry)
    await screen.findByRole('heading', { name: 'Connect an agent over MCP' })
    const text = document.body.textContent
    expect(text).toContain('https://registry.example.test/mcp')
    expect(text).toContain('"Authorization": "Bearer <YOUR_API_TOKEN>"')
    expect(text).toContain(
      'claude mcp add --transport http alexandria https://registry.example.test/mcp --header "Authorization: Bearer <YOUR_API_TOKEN>"',
    )
    expect(text).toContain('alx pull pdf-forms@1.2.0')
    expect(text).toContain("export ALX_URL=https://registry.example.test")
    await waitFor(() =>
      expect(document.body.textContent).toContain(
        'https://registry.example.test/api/v1/skills/pdf-forms/versions/1.2.0/download',
      ),
    )
    expect(document.body.textContent).toContain('-o pdf-forms-1.2.0.zip')
  })

  it('never puts the session token in a snippet', async () => {
    setup({ hash: '#/library/pdf-forms?tab=use' })
    render(Entry)
    await screen.findByRole('heading', { name: 'Connect an agent over MCP' })
    expect(document.body.textContent).not.toContain('jwt-session-token')
  })

  it('copies a snippet', async () => {
    const { user } = setup({ hash: '#/library/pdf-forms?tab=use' })
    render(Entry)
    await screen.findByRole('heading', { name: 'Connect an agent over MCP' })
    await user.click(screen.getByRole('button', { name: /Copy Claude Code/ }))
    expect(await navigator.clipboard.readText()).toMatch(/^claude mcp add --transport http alexandria /)
  })

  it('follows the selected version', async () => {
    const { user } = setup({ hash: '#/library/pdf-forms?tab=use' })
    render(Entry)
    await screen.findByRole('heading', { name: 'Connect an agent over MCP' })
    await user.selectOptions(screen.getByLabelText('Version'), '1.1.0')
    await waitFor(() => expect(document.body.textContent).toContain('alx pull pdf-forms@1.1.0'))
    expect(window.location.hash).toBe('#/library/pdf-forms?tab=use&version=1.1.0')
  })

  it('warns about draft versions and private registries', async () => {
    setup({
      hash: '#/library/pdf-forms?tab=use&version=1.0.0-rc.1',
      info: registryInfo({ public_read: false }),
    })
    render(Entry)
    expect(await screen.findByText('This version is a draft')).toBeInTheDocument()
    await screen.findByText(/This registry is private, so every request needs the header/)
  })

  it('warns about deprecated versions', async () => {
    setup({
      hash: '#/library/pdf-forms?tab=use&version=0.9.0',
      entry: entryDetail({}, [...VERSIONS, versionSummary({ id: 'v9', version: '0.9.0', status: 'deprecated' })]),
    })
    render(Entry)
    expect(await screen.findByText('This version is deprecated')).toBeInTheDocument()
  })

  it('still produces snippets when the registry information cannot be loaded', async () => {
    setup({
      hash: '#/library/pdf-forms?tab=use',
      handlers: [{ method: 'GET', path: '/info', respond: () => errorResponse(500, 'INTERNAL_ERROR', 'x') }],
    })
    render(Entry)
    await screen.findByRole('heading', { name: 'Connect an agent over MCP' })
    expect(document.body.textContent).toContain(`${window.location.origin}/mcp`)
  })
})
