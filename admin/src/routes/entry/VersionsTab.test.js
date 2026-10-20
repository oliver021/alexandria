import { render, screen, waitFor, within } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { toasts } from '../../lib/toast.svelte.js'
import {
  binaryResponse,
  entryDetail,
  errorResponse,
  jsonResponse,
  mockFetch,
  resetApp,
  scrolledIntoView,
  signIn,
  versionSummary,
} from '../../test/helpers.js'
import VersionsTab from './VersionsTab.svelte'

const VERSIONS = [
  versionSummary({ id: 'v3', version: '1.2.0', status: 'published' }),
  versionSummary({ id: 'v2', version: '1.1.0', status: 'published', published_at: '2026-02-01T00:00:00Z' }),
  versionSummary({ id: 'v1', version: '1.0.0', status: 'draft', published_at: null }),
  versionSummary({
    id: 'v0',
    version: '0.9.0',
    status: 'deprecated',
    deprecated_at: '2026-01-15T00:00:00Z',
  }),
]

function setup({ versions = VERSIONS, canManage = true, handlers = [] } = {}) {
  const entry = entryDetail({ latest_version: '1.2.0' }, versions)
  const onrefresh = vi.fn().mockResolvedValue(undefined)
  const onselecttab = vi.fn()
  const http = mockFetch([
    ...handlers,
    { method: 'POST', path: /\/versions\/[^/]+\/publish$/, respond: () => jsonResponse(versionSummary()) },
    { method: 'POST', path: /\/versions\/[^/]+\/deprecate$/, respond: () => jsonResponse(versionSummary()) },
  ])
  const user = userEvent.setup()
  render(VersionsTab, { props: { entry, canManage, onrefresh, onselecttab } })
  return { entry, onrefresh, onselecttab, http, user }
}

const statusOf = (row) => row.querySelector('td .badge').textContent.trim()

const rowFor = (version) => screen.getByRole('row', { name: new RegExp(`^${version.replace(/\./g, '\\.')}`) })

beforeEach(() => {
  resetApp()
  signIn()
})

afterEach(resetApp)

describe('VersionsTab: table', () => {
  it('lists the versions in order with status, size and checksum', () => {
    setup()
    const table = screen.getByRole('table', { name: /Versions of PDF Forms/ })
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows.map((row) => within(row).getByRole('rowheader').textContent.trim().split(/\s+/)[0])).toEqual([
      '1.2.0',
      '1.1.0',
      '1.0.0',
      '0.9.0',
    ])
    expect(within(rowFor('1.2.0')).getByText('latest')).toBeInTheDocument()
    expect(within(rowFor('1.1.0')).queryByText('latest')).not.toBeInTheDocument()
    expect(statusOf(rowFor('1.2.0'))).toBe('Published')
    expect(statusOf(rowFor('1.0.0'))).toBe('Draft')
    expect(statusOf(rowFor('0.9.0'))).toBe('Deprecated')
    expect(within(rowFor('1.2.0')).getByText('2 KiB')).toBeInTheDocument()
    expect(within(rowFor('1.2.0')).getByText('a'.repeat(12))).toBeInTheDocument()
  })

  it('links each version to its files', () => {
    setup()
    expect(within(rowFor('1.1.0')).getByRole('link', { name: /Files of version 1\.1\.0/ })).toHaveAttribute(
      'href',
      '#/library/pdf-forms?tab=files&version=1.1.0',
    )
  })

  it('offers the right actions for every status', () => {
    setup()
    expect(within(rowFor('1.2.0')).getByRole('button', { name: /Deprecate version 1\.2\.0/ })).toBeInTheDocument()
    expect(within(rowFor('1.2.0')).queryByRole('button', { name: /Publish/ })).not.toBeInTheDocument()
    expect(within(rowFor('1.0.0')).getByRole('button', { name: 'Publish version 1.0.0' })).toBeInTheDocument()
    expect(within(rowFor('0.9.0')).getByRole('button', { name: 'Publish again version 0.9.0' })).toBeInTheDocument()
    expect(within(rowFor('0.9.0')).queryByRole('button', { name: /Deprecate/ })).not.toBeInTheDocument()
  })

  it('hides the state changes from people who cannot manage the entry', () => {
    setup({ canManage: false })
    expect(screen.queryByRole('button', { name: /^Publish/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Deprecate/ })).not.toBeInTheDocument()
    expect(screen.getByText(/Only the owner of this entry and admins can publish or deprecate/)).toBeInTheDocument()
    expect(within(rowFor('1.2.0')).getByRole('button', { name: /Download/ })).toBeInTheDocument()
  })

  it('has an empty state when no version is visible', () => {
    setup({ versions: [] })
    expect(screen.getByText('No versions you can see')).toBeInTheDocument()
  })
})

describe('VersionsTab: publishing', () => {
  it('asks for confirmation before publishing a draft', async () => {
    const { http, onrefresh, user } = setup()
    await user.click(screen.getByRole('button', { name: 'Publish version 1.0.0' }))
    const dialog = await screen.findByRole('dialog', { name: 'Publish version 1.0.0?' })
    expect(http.calls).toHaveLength(0)
    await user.click(within(dialog).getByRole('button', { name: 'Publish version' }))
    await waitFor(() => expect(onrefresh).toHaveBeenCalledTimes(1))
    expect(http.calls).toHaveLength(1)
    expect(http.calls[0].method).toBe('POST')
    expect(http.calls[0].path).toBe('/skills/pdf-forms/versions/1.0.0/publish')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(toasts.items.some((item) => item.message === 'Version 1.0.0 is published.')).toBe(true)
    await waitFor(() => expect(document.getElementById('versions-region')).toHaveFocus())
  })

  it('can publish a deprecated version again', async () => {
    const { http, user } = setup()
    await user.click(screen.getByRole('button', { name: 'Publish again version 0.9.0' }))
    await user.click(await screen.findByRole('button', { name: 'Publish version' }))
    await waitFor(() => expect(http.calls).toHaveLength(1))
    expect(http.calls[0].path).toBe('/skills/pdf-forms/versions/0.9.0/publish')
  })

  it('does nothing when the dialog is cancelled', async () => {
    const { http, onrefresh, user } = setup()
    await user.click(screen.getByRole('button', { name: 'Publish version 1.0.0' }))
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(http.calls).toHaveLength(0)
    expect(onrefresh).not.toHaveBeenCalled()
  })

  it('explains a failure to publish', async () => {
    const { onrefresh, user } = setup({
      handlers: [
        {
          method: 'POST',
          path: /\/publish$/,
          respond: () => errorResponse(403, 'FORBIDDEN', 'Only the owner of this entry or an admin can change it'),
        },
      ],
    })
    await user.click(screen.getByRole('button', { name: 'Publish version 1.0.0' }))
    await user.click(await screen.findByRole('button', { name: 'Publish version' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The version was not published')
    expect(alert).toHaveTextContent('Only the owner of this entry or an admin can change it')
    expect(onrefresh).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})

describe('VersionsTab: deprecating', () => {
  it('asks for confirmation before deprecating', async () => {
    const { http, onrefresh, user } = setup()
    await user.click(screen.getByRole('button', { name: 'Deprecate version 1.1.0' }))
    const dialog = await screen.findByRole('dialog', { name: 'Deprecate version 1.1.0?' })
    expect(dialog).toHaveTextContent(/no longer be/)
    expect(within(dialog).queryByText(/only published version/)).not.toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Deprecate version' }))
    await waitFor(() => expect(onrefresh).toHaveBeenCalledTimes(1))
    expect(http.calls[0].path).toBe('/skills/pdf-forms/versions/1.1.0/deprecate')
    expect(toasts.items.some((item) => item.message === 'Version 1.1.0 is deprecated.')).toBe(true)
  })

  it('warns in advance when it is the only published version', async () => {
    const { user } = setup({
      versions: [
        versionSummary({ id: 'v3', version: '1.2.0', status: 'published' }),
        versionSummary({ id: 'v1', version: '1.0.0', status: 'draft' }),
      ],
    })
    await user.click(screen.getByRole('button', { name: 'Deprecate version 1.2.0' }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent(/only published version, so the registry will refuse/)
  })

  it('handles the 409 of the only published version without refreshing', async () => {
    const { onrefresh, onselecttab, user } = setup({
      versions: [versionSummary({ id: 'v3', version: '1.2.0', status: 'published' })],
      handlers: [
        {
          method: 'POST',
          path: /\/deprecate$/,
          respond: () =>
            errorResponse(409, 'CONFLICT', 'The only published version of an entry cannot be deprecated'),
        },
      ],
    })
    await user.click(screen.getByRole('button', { name: 'Deprecate version 1.2.0' }))
    await user.click(await screen.findByRole('button', { name: 'Deprecate version' }))
    const alert = (await screen.findByText('This is the only published version')).closest('.alert')
    expect(alert).toHaveTextContent('This is the only published version')
    expect(alert).toHaveTextContent(/Publish another version first, or hide the whole entry/)
    expect(alert).toHaveTextContent('The only published version of an entry cannot be deprecated')
    expect(onrefresh).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(statusOf(rowFor('1.2.0'))).toBe('Published')

    await user.click(within(alert).getByRole('button', { name: 'Go to Summary' }))
    expect(onselecttab).toHaveBeenCalledWith('summary')

    await user.click(within(alert).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByText('This is the only published version')).not.toBeInTheDocument()
  })

  it('reports other errors as failures, not as the 409 case', async () => {
    const { user } = setup({
      handlers: [
        { method: 'POST', path: /\/deprecate$/, respond: () => errorResponse(500, 'INTERNAL_ERROR', 'boom') },
      ],
    })
    await user.click(screen.getByRole('button', { name: 'Deprecate version 1.1.0' }))
    await user.click(await screen.findByRole('button', { name: 'Deprecate version' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The version was not deprecated')
    expect(screen.queryByText('This is the only published version')).not.toBeInTheDocument()
  })
})

describe('VersionsTab: bringing a failure into view', () => {
  // The notice is drawn above the table and the button that caused it is in one of the rows, which
  // can be well below it. The dialog gives the focus back to that button when it closes, so the page
  // has to take it again.
  const CENTERED = { block: 'center', inline: 'nearest' }

  it('moves the focus to the notice and scrolls it into view when publishing fails', async () => {
    const { user } = setup({
      handlers: [
        { method: 'POST', path: /\/publish$/, respond: () => errorResponse(403, 'FORBIDDEN', 'Not yours') },
      ],
    })
    await user.click(screen.getByRole('button', { name: 'Publish version 1.0.0' }))
    await user.click(await screen.findByRole('button', { name: 'Publish version' }))
    const alert = await screen.findByRole('alert')
    await waitFor(() => expect(alert).toHaveFocus())
    expect(alert).toHaveAttribute('id', 'versions-notice')
    expect(scrolledIntoView()).toEqual([{ element: alert, options: CENTERED }])
  })

  it('does the same for the warning about the only published version', async () => {
    const { user } = setup({
      versions: [versionSummary({ id: 'v3', version: '1.2.0', status: 'published' })],
      handlers: [
        {
          method: 'POST',
          path: /\/deprecate$/,
          respond: () =>
            errorResponse(409, 'CONFLICT', 'The only published version of an entry cannot be deprecated'),
        },
      ],
    })
    await user.click(screen.getByRole('button', { name: 'Deprecate version 1.2.0' }))
    await user.click(await screen.findByRole('button', { name: 'Deprecate version' }))
    const notice = (await screen.findByText('This is the only published version')).closest('.alert')
    await waitFor(() => expect(notice).toHaveFocus())
    expect(scrolledIntoView()).toEqual([{ element: notice, options: CENTERED }])
  })

  it('scrolls nowhere when the change works', async () => {
    const { onrefresh, user } = setup()
    await user.click(screen.getByRole('button', { name: 'Publish version 1.0.0' }))
    await user.click(await screen.findByRole('button', { name: 'Publish version' }))
    await waitFor(() => expect(onrefresh).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(document.getElementById('versions-region')).toHaveFocus())
    expect(scrolledIntoView()).toEqual([])
  })
})

describe('VersionsTab: downloading', () => {
  const originalCreate = URL.createObjectURL
  let saved

  beforeEach(() => {
    saved = []
    URL.createObjectURL = vi.fn(() => 'blob:test')
    URL.revokeObjectURL = vi.fn()
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function click() {
      saved.push(this.download)
    })
  })

  afterEach(() => {
    URL.createObjectURL = originalCreate
  })

  async function sha256(bytes) {
    const digest = await crypto.subtle.digest('SHA-256', new Uint8Array(bytes))
    return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('')
  }

  it('downloads the zip through fetch, with the token only in the header', async () => {
    const bytes = [80, 75, 3, 4, 1, 2, 3]
    const checksum = await sha256(bytes)
    const { http, user } = setup({
      handlers: [
        {
          method: 'GET',
          path: '/skills/pdf-forms/versions/1.2.0/download',
          respond: () => binaryResponse(bytes, { 'x-content-sha256': checksum }),
        },
      ],
    })
    await user.click(within(rowFor('1.2.0')).getByRole('button', { name: /Download version 1\.2\.0/ }))
    await waitFor(() => expect(saved).toEqual(['pdf-forms-1.2.0.zip']))
    const call = http.calls[0]
    expect(call.url.href).not.toContain('jwt-session-token')
    expect(Object.values(call.headers)).toContain('Bearer jwt-session-token')
    expect(toasts.items.some((item) => item.message === 'Downloaded pdf-forms-1.2.0.zip. Its SHA-256 matches.')).toBe(true)
  })

  it('saves the file but says so when the checksum cannot be verified', async () => {
    const { user } = setup({
      handlers: [
        {
          method: 'GET',
          path: /\/download$/,
          respond: () => binaryResponse([1, 2, 3], { 'x-content-sha256': 'a'.repeat(64) }),
        },
      ],
    })
    vi.stubGlobal('crypto', {})
    await user.click(within(rowFor('1.2.0')).getByRole('button', { name: /Download version 1\.2\.0/ }))
    await waitFor(() => expect(saved).toEqual(['pdf-forms-1.2.0.zip']))
    const warning = toasts.items.find((item) => item.tone === 'warning')
    expect(warning?.message).toMatch(/could not verify its SHA-256/)
  })

  it('refuses to save a download that does not match its checksum', async () => {
    const { user } = setup({
      handlers: [
        {
          method: 'GET',
          path: /\/download$/,
          respond: () => binaryResponse([1, 2, 3], { 'x-content-sha256': 'f'.repeat(64) }),
        },
      ],
    })
    await user.click(within(rowFor('1.2.0')).getByRole('button', { name: /Download version 1\.2\.0/ }))
    await waitFor(() => expect(toasts.items.some((item) => item.tone === 'error')).toBe(true))
    expect(saved).toEqual([])
    expect(toasts.items.find((item) => item.tone === 'error').message).toMatch(/does not match its published checksum/)
  })

  it('reports a failed download', async () => {
    const { user } = setup({
      handlers: [
        { method: 'GET', path: /\/download$/, respond: () => errorResponse(404, 'NOT_FOUND', 'gone') },
      ],
    })
    await user.click(within(rowFor('1.2.0')).getByRole('button', { name: /Download version 1\.2\.0/ }))
    await waitFor(() => expect(toasts.items.some((item) => item.tone === 'error')).toBe(true))
    expect(saved).toEqual([])
  })
})
