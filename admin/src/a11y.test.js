import { render, screen, waitFor } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import axe from 'axe-core'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import App from './App.svelte'
import { router } from './lib/router.svelte.js'
import {
  ADMIN,
  FakeXHR,
  PUBLISHER,
  entryDetail,
  errorResponse,
  installFakeXHR,
  jsonResponse,
  mockFetch,
  registryInfo,
  resetApp,
  signIn,
  versionSummary,
} from './test/helpers.js'

const VERSIONS = [
  versionSummary({ id: 'v2', version: '1.2.0', status: 'published' }),
  versionSummary({ id: 'v1', version: '1.0.0', status: 'draft', published_at: null }),
]

function entry() {
  return {
    id: 'e1',
    name: 'pdf-forms',
    display_name: 'PDF Forms',
    description: 'Fill and read PDF forms.',
    kind: 'skill',
    category: 'documents',
    tags: ['pdf', 'forms'],
    status: 'published',
    latest_version: '1.2.0',
    owner_id: ADMIN.id,
    created_at: '2026-02-01T10:00:00Z',
    updated_at: '2026-03-01T10:00:00Z',
  }
}

const USER_PATHS = {
  '/api/v1/users': { get: { summary: 'List users' }, post: { summary: 'Create a user' } },
}

function apiDescription(extraPaths = {}) {
  return { openapi: '3.1.0', paths: { ...USER_PATHS, ...extraPaths } }
}

const DEACTIVATE_PATH = {
  '/api/v1/users/{user_id}/deactivate': { post: { summary: 'Deactivate a user' } },
}

function setup(hash, extra = []) {
  window.history.replaceState(null, '', hash)
  return mockFetch([
    ...extra,
    { method: 'GET', path: '/openapi.json', respond: () => jsonResponse(apiDescription(DEACTIVATE_PATH)) },
    { method: 'GET', path: '/auth/me', respond: () => jsonResponse(ADMIN) },
    {
      method: 'GET',
      path: '/skills',
      respond: ({ query }) =>
        jsonResponse({
          items: query.get('limit') === '1' ? [] : [entry()],
          total: query.get('limit') === '1' ? 3 : 1,
          limit: Number(query.get('limit') ?? 20),
          offset: 0,
        }),
    },
    { method: 'GET', path: '/skills/pdf-forms', respond: () => jsonResponse({ ...entry(), versions: VERSIONS }) },
    {
      method: 'GET',
      path: /^\/skills\/pdf-forms\/versions\/[^/]+$/,
      respond: () => jsonResponse({ ...VERSIONS[0], metadata: { author: 'Ada' }, download_url: 'https://r.test/d' }),
    },
    {
      method: 'GET',
      path: /\/files$/,
      respond: () =>
        jsonResponse({
          name: 'pdf-forms',
          version: '1.2.0',
          files: [
            { path: 'SKILL.md', size: 10, sha256: 'a'.repeat(64), media_type: 'text/markdown', text: true },
            { path: 'data/x.bin', size: 20, sha256: 'b'.repeat(64), media_type: 'application/octet-stream', text: false },
          ],
        }),
    },
    {
      method: 'GET',
      path: /\/files\/SKILL\.md$/,
      respond: () =>
        jsonResponse({ path: 'SKILL.md', size: 10, sha256: 'a'.repeat(64), media_type: 'text/markdown', text: true, content: '# Title', truncated: false, encoding: 'utf-8' }),
    },
    { method: 'GET', path: '/info', respond: () => jsonResponse(registryInfo()) },
    { method: 'GET', path: '/tags', respond: () => jsonResponse([{ name: 'pdf', count: 2 }]) },
    { method: 'GET', path: '/categories', respond: () => jsonResponse([{ name: 'documents', count: 2 }]) },
    {
      method: 'GET',
      path: '/tokens',
      respond: () =>
        jsonResponse([
          { id: 't1', name: 'CI', prefix: 'alx_abcdefgh', scope: 'read', created_at: '2026-03-01T10:00:00Z', expires_at: null, last_used_at: null, revoked_at: null },
        ]),
    },
    { method: 'POST', path: '/tokens', respond: () => jsonResponse({ id: 't2', name: 'New', prefix: 'alx_zzzzzzzz', scope: 'read', created_at: '2026-03-01T10:00:00Z', expires_at: null, last_used_at: null, revoked_at: null, token: 'alx_secretsecretsecretsecretsecretsecretsecret' }, 201) },
    { method: 'GET', path: '/users', respond: () => jsonResponse([ADMIN, PUBLISHER]) },
  ])
}

async function violations(container) {
  const results = await axe.run(container, {
    rules: { 'color-contrast': { enabled: false }, region: { enabled: false } },
  })
  return results.violations.map(
    (item) => `${item.id}: ${item.help} :: ${item.nodes.map((node) => node.target.join(' ')).join(' | ')}`,
  )
}

beforeEach(() => {
  resetApp()
  signIn(ADMIN)
})

afterEach(() => {
  router.stop()
  resetApp()
})

describe('accessibility audit with axe', () => {
  it.each([
    ['dashboard', '#/', 'Dashboard'],
    ['library', '#/library', 'Library'],
    ['entry summary', '#/library/pdf-forms', 'PDF Forms'],
    ['entry versions', '#/library/pdf-forms?tab=versions', 'PDF Forms'],
    ['entry files', '#/library/pdf-forms?tab=files&file=SKILL.md', 'PDF Forms'],
    ['entry binary file', '#/library/pdf-forms?tab=files&file=data%2Fx.bin', 'PDF Forms'],
    ['entry use', '#/library/pdf-forms?tab=use', 'PDF Forms'],
    ['upload', '#/upload', 'Upload package'],
    ['tokens', '#/tokens', 'API tokens'],
    ['users', '#/users', 'Users'],
    ['not found', '#/nowhere', 'Page not found'],
  ])('%s has no violations', async (_name, hash, heading) => {
    setup(hash)
    const { container } = render(App)
    await screen.findByRole('heading', { level: 1, name: heading })
    await waitFor(() => expect(container.querySelector('[aria-busy="true"], .spinner')).toBeNull(), { timeout: 3000 })
    expect(await violations(container)).toEqual([])
  })

  it('the login page has no violations, with and without an error', async () => {
    resetApp()
    setup('#/login', [
      { method: 'POST', path: '/auth/login', respond: () => errorResponse(401, 'UNAUTHORIZED', 'Invalid credentials') },
    ])
    const { container } = render(App)
    const user = userEvent.setup()
    await screen.findByRole('button', { name: 'Sign in' })
    expect(await violations(container)).toEqual([])
    await user.type(screen.getByLabelText('Username'), 'ada')
    await user.type(screen.getByLabelText('Password'), 'wrong')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    await screen.findByRole('alert')
    expect(await violations(container)).toEqual([])
  })

  it('an open confirmation dialog has no violations', async () => {
    setup('#/library/pdf-forms?tab=versions')
    const { container } = render(App)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /Publish version 1\.0\.0/ }))
    await screen.findByRole('dialog')
    expect(await violations(container)).toEqual([])
  })

  it('the one-time token panel and the form errors have no violations', async () => {
    setup('#/tokens')
    const { container } = render(App)
    const user = userEvent.setup()
    await screen.findByRole('heading', { level: 1, name: 'API tokens' })
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    await screen.findByText(/Give the token a name/)
    expect(await violations(container)).toEqual([])
    await user.type(screen.getByLabelText('Name'), 'New')
    await user.click(screen.getByRole('button', { name: 'Create token' }))
    await screen.findByLabelText(/Token .New./)
    expect(await violations(container)).toEqual([])
  })

  it('the user list has no violations with its deactivation actions', async () => {
    setup('#/users')
    const { container } = render(App)
    await screen.findByRole('button', { name: 'Deactivate grace' })
    expect(await violations(container)).toEqual([])
  })

  it('an open deactivation dialog has no violations', async () => {
    setup('#/users')
    const { container } = render(App)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Deactivate grace' }))
    await screen.findByRole('dialog', { name: 'Deactivate grace?' })
    expect(await violations(container)).toEqual([])
  })

  it('the user list has no violations when the registry cannot deactivate accounts', async () => {
    setup('#/users', [
      { method: 'GET', path: '/openapi.json', respond: () => jsonResponse(apiDescription()) },
    ])
    const { container } = render(App)
    await screen.findByText(/no operation to deactivate accounts/)
    expect(screen.queryByRole('button', { name: /Deactivate/ })).not.toBeInTheDocument()
    expect(await violations(container)).toEqual([])
  })

  it('the user list has no violations when the API description cannot be read', async () => {
    setup('#/users', [
      { method: 'GET', path: '/openapi.json', respond: () => errorResponse(500, 'INTERNAL_ERROR', 'x') },
    ])
    const { container } = render(App)
    await screen.findByText(/could not read the description of the registry's API/)
    expect(await violations(container)).toEqual([])
  })

  it('upload problems and failures have no violations', async () => {
    setup('#/upload')
    const { container } = render(App)
    const user = userEvent.setup({ applyAccept: false })
    await screen.findByRole('heading', { level: 1, name: 'Upload package' })
    await user.upload(screen.getByLabelText('Package file'), new File(['x'], 'notes.txt'))
    await screen.findByText(/is not a \.zip file/)
    expect(await violations(container)).toEqual([])
  })

  it('a failed upload, whose explanation takes the focus, has no violations', async () => {
    setup('#/upload')
    installFakeXHR()
    const { container } = render(App)
    const user = userEvent.setup({ applyAccept: false })
    await screen.findByRole('heading', { level: 1, name: 'Upload package' })
    await user.upload(screen.getByLabelText('Package file'), new File(['x'], 'pdf-forms.zip'))
    await user.click(screen.getByRole('button', { name: 'Upload package' }))
    FakeXHR.instances[0].respond(422, {
      error: { code: 'INVALID_PACKAGE', message: 'No entry point', reason: 'MISSING_ENTRYPOINT' },
    })
    const alert = await screen.findByRole('alert')
    await waitFor(() => expect(alert).toHaveFocus())
    expect(await violations(container)).toEqual([])
  })

  it('error and empty states have no violations', async () => {
    setup('#/library', [
      { method: 'GET', path: '/skills', respond: () => errorResponse(500, 'INTERNAL_ERROR', 'x') },
    ])
    const { container } = render(App)
    await screen.findByText('The registry failed')
    expect(await violations(container)).toEqual([])
  })

  it('the publisher view has no violations', async () => {
    resetApp()
    signIn(PUBLISHER)
    setup('#/library/pdf-forms')
    const { container } = render(App)
    await screen.findByRole('heading', { level: 1, name: 'PDF Forms' })
    expect(await violations(container)).toEqual([])
  })
})

describe('axe sanity check', () => {
  it('detects a real problem, so a clean result means something', async () => {
    document.body.innerHTML = '<img src="x.png"><button></button>'
    const found = await violations(document.body)
    expect(found.some((line) => line.startsWith('image-alt'))).toBe(true)
    expect(found.some((line) => line.startsWith('button-name'))).toBe(true)
    document.body.innerHTML = ''
  })
})
