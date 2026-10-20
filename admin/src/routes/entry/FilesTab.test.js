import { render, screen, within } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  entryDetail,
  errorResponse,
  jsonResponse,
  mockFetch,
  resetApp,
  signIn,
  versionSummary,
} from '../../test/helpers.js'
import FilesTab from './FilesTab.svelte'

const VERSIONS = [
  versionSummary({ id: 'v2', version: '1.2.0' }),
  versionSummary({ id: 'v1', version: '1.1.0' }),
]

function file(path, overrides = {}) {
  return { path, size: 10, sha256: 'a'.repeat(64), media_type: 'text/plain', text: true, ...overrides }
}

function setup({ files = [file('SKILL.md')], version = '1.2.0', selected = null, handlers = [] } = {}) {
  const props = {
    entry: entryDetail({ latest_version: '1.2.0' }, VERSIONS),
    version,
    file: selected,
    onversion: vi.fn(),
    onfile: vi.fn(),
  }
  const http = mockFetch([
    ...handlers,
    {
      method: 'GET',
      path: /\/files$/,
      respond: () => jsonResponse({ name: 'pdf-forms', version: version ?? '1.2.0', files }),
    },
  ])
  const view = render(FilesTab, { props })
  return { props, http, view, user: userEvent.setup() }
}

beforeEach(() => {
  resetApp()
  signIn()
})

afterEach(resetApp)

describe('FilesTab', () => {
  it('summarizes the package and reports the chosen file', async () => {
    const { props, user } = setup({
      files: [file('SKILL.md', { size: 100 }), file('data/a.csv', { size: 2048 })],
    })
    expect(await screen.findByText('2 files, 2.1 KiB unpacked')).toBeInTheDocument()
    await user.click(screen.getByRole('treeitem', { name: /SKILL\.md/ }))
    expect(props.onfile).toHaveBeenCalledWith('SKILL.md')
  })

  it('reports a version change', async () => {
    const { props, user } = setup()
    await screen.findByRole('tree')
    await user.selectOptions(screen.getByLabelText('Version'), '1.1.0')
    expect(props.onversion).toHaveBeenCalledWith('1.1.0')
  })

  it('shows a loading state, then an error with a retry', async () => {
    const user = userEvent.setup()
    let fail = true
    mockFetch([
      {
        method: 'GET',
        path: /\/files$/,
        respond: () =>
          fail
            ? errorResponse(500, 'STORAGE_ERROR', 'x')
            : jsonResponse({ name: 'pdf-forms', version: '1.2.0', files: [file('SKILL.md')] }),
      },
    ])
    render(FilesTab, {
      props: {
        entry: entryDetail({ latest_version: '1.2.0' }, VERSIONS),
        version: '1.2.0',
        file: null,
        onversion: vi.fn(),
        onfile: vi.fn(),
      },
    })
    expect(screen.getByText(/Loading files/)).toBeInTheDocument()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/stored data of this version is missing or damaged/)
    fail = false
    await user.click(within(alert).getByRole('button', { name: /Try again/ }))
    expect(await screen.findByRole('tree')).toBeInTheDocument()
  })

  it('has an empty state for a version without files', async () => {
    setup({ files: [] })
    expect(await screen.findByText('This version has no files')).toBeInTheDocument()
    expect(screen.queryByRole('tree')).not.toBeInTheDocument()
  })

  it('has an empty state when there is no version to browse', () => {
    const http = mockFetch([])
    render(FilesTab, {
      props: {
        entry: entryDetail({ latest_version: null, status: 'draft' }, []),
        version: null,
        file: null,
        onversion: vi.fn(),
        onfile: vi.fn(),
      },
    })
    expect(screen.getByText('No version to browse')).toBeInTheDocument()
    expect(http.calls).toHaveLength(0)
  })

  it('invites the user to pick a file and keeps a missing file from breaking the page', async () => {
    const first = setup()
    expect(await screen.findByText('Select a file')).toBeInTheDocument()
    first.view.unmount()
    setup({ selected: 'gone.md' })
    expect(await screen.findByText('That file is not in this version')).toBeInTheDocument()
  })

  it('expands every folder of a small package and collapses on demand', async () => {
    const { user } = setup({ files: [file('a/b/c.txt'), file('a/d.txt')] })
    expect(await screen.findByRole('treeitem', { name: /c\.txt/ })).toBeInTheDocument()
    await user.click(screen.getByRole('treeitem', { name: /^.*\ba\b.*2 files/ }))
    expect(screen.queryByRole('treeitem', { name: /c\.txt/ })).not.toBeInTheDocument()
  })

  it('only opens the first level of a big package', async () => {
    const files = Array.from({ length: 70 }, (_, index) => file(`pkg/sub${index % 5}/f${index}.txt`))
    setup({ files })
    await screen.findByRole('tree')
    const items = screen.getAllByRole('treeitem').map((item) => item.dataset.path)
    expect(items).toEqual(['pkg', 'pkg/sub0', 'pkg/sub1', 'pkg/sub2', 'pkg/sub3', 'pkg/sub4'])
  })

  it('opens the folders that lead to the selected file in a big package', async () => {
    const files = Array.from({ length: 70 }, (_, index) => file(`pkg/sub${index % 5}/f${index}.txt`))
    setup({
      files,
      selected: 'pkg/sub3/f3.txt',
      handlers: [
        {
          method: 'GET',
          path: /files\/pkg/,
          respond: () => jsonResponse({ path: 'pkg/sub3/f3.txt', size: 1, sha256: 'a'.repeat(64), media_type: 'text/plain', text: true, content: 'x', truncated: false, encoding: 'utf-8' }),
        },
      ],
    })
    const item = await screen.findByRole('treeitem', { name: /f3\.txt/ })
    expect(item).toHaveAttribute('aria-selected', 'true')
  })
})
