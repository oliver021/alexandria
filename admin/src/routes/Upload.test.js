import { fireEvent, render, screen, waitFor, within } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { PACKAGE_REASONS } from '../lib/reasons.js'
import { toasts } from '../lib/toast.svelte.js'
import {
  FakeXHR,
  entrySummary,
  errorResponse,
  installFakeXHR,
  jsonResponse,
  mockFetch,
  registryInfo,
  resetApp,
  scrolledIntoView,
  signIn,
  versionSummary,
} from '../test/helpers.js'
import Upload from './Upload.svelte'

const MIB = 1024 * 1024

function makeFile(name = 'pdf-forms.zip', size = 200) {
  const file = new File([new Uint8Array(Math.min(size, 256))], name, { type: 'application/zip' })
  Object.defineProperty(file, 'size', { value: size })
  return file
}

function uploadResult(overrides = {}) {
  return {
    skill: entrySummary({ name: 'pdf-forms', display_name: 'PDF Forms' }),
    version: versionSummary({ version: '1.2.0', status: 'draft' }),
    ...overrides,
  }
}

async function setup({ infoResponse, ready = /Up to 25 MiB zipped/ } = {}) {
  mockFetch([
    {
      method: 'GET',
      path: '/info',
      respond: infoResponse ?? (() => jsonResponse(registryInfo())),
    },
  ])
  installFakeXHR()
  const user = userEvent.setup({ applyAccept: false })
  const view = render(Upload)
  await screen.findByText(ready)
  return { user, view, input: screen.getByLabelText('Package file') }
}

const submitButton = () => screen.getByRole('button', { name: /Upload package|Uploading/ })

beforeEach(() => {
  resetApp()
  signIn()
})

afterEach(resetApp)

describe('Upload: checks before sending', () => {
  it('shows the limits announced by the registry', async () => {
    await setup()
    expect(screen.getByText(/Up to 25 MiB zipped, 2,000 files and 100 MiB unpacked/)).toBeInTheDocument()
  })

  it('refuses a file that is not a zip', async () => {
    const { user, input } = await setup()
    await user.upload(input, makeFile('notes.txt', 100))
    expect(await screen.findByRole('alert')).toHaveTextContent('"notes.txt" is not a .zip file')
    expect(submitButton()).toBeDisabled()
    expect(FakeXHR.instances).toHaveLength(0)
  })

  it('refuses a file larger than the limit of the registry', async () => {
    const { user, input } = await setup()
    await user.upload(input, makeFile('big.zip', 30 * MIB))
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The file is 30 MiB, larger than the 25 MiB this registry accepts.',
    )
    expect(submitButton()).toBeDisabled()
  })

  it('refuses an empty file', async () => {
    const { user, input } = await setup()
    await user.upload(input, makeFile('empty.zip', 0))
    expect(await screen.findByRole('alert')).toHaveTextContent('The file is empty.')
    expect(submitButton()).toBeDisabled()
  })

  it('accepts a file of exactly the maximum size', async () => {
    const { user, input } = await setup()
    await user.upload(input, makeFile('limit.zip', 25 * MIB))
    expect(await screen.findByText('limit.zip')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(submitButton()).toBeEnabled()
  })

  it('needs a file before it can be submitted', async () => {
    await setup()
    expect(submitButton()).toBeDisabled()
  })

  it('checks the version when the field loses focus', async () => {
    const { user, input } = await setup()
    await user.upload(input, makeFile())
    const version = screen.getByLabelText(/Version/)
    await user.type(version, '1.0')
    await user.tab()
    expect(await screen.findByText(/Use a semantic version such as 1\.2\.0/)).toBeInTheDocument()
    expect(submitButton()).toBeDisabled()
    await user.clear(version)
    await user.type(version, '1.0.0+build')
    expect(await screen.findByText(/Build metadata/)).toBeInTheDocument()
    await user.clear(version)
    await user.type(version, '1.0.0-rc.1')
    await waitFor(() => expect(screen.queryByText(/Use a semantic version/)).not.toBeInTheDocument())
    expect(submitButton()).toBeEnabled()
  })

  it('still works when the limits cannot be loaded', async () => {
    const { user, input } = await setup({
      infoResponse: () => errorResponse(500, 'INTERNAL_ERROR', 'x'),
      ready: /enforces its own size limits/,
    })
    await user.upload(input, makeFile('huge.zip', 500 * MIB))
    expect(submitButton()).toBeEnabled()
  })

  it('accepts a dropped file and uses only the first one', async () => {
    const { view } = await setup()
    const zone = view.container.querySelector('.dropzone')
    await fireEvent.drop(zone, { dataTransfer: { files: [makeFile('first.zip'), makeFile('second.zip')] } })
    expect(await screen.findByText('first.zip')).toBeInTheDocument()
    expect(screen.queryByText('second.zip')).not.toBeInTheDocument()
    expect(screen.getByText(/only the first file was used/)).toBeInTheDocument()
  })

  it('highlights the drop zone while dragging', async () => {
    const { view } = await setup()
    const zone = view.container.querySelector('.dropzone')
    await fireEvent.dragEnter(zone, { dataTransfer: { files: [] } })
    expect(zone).toHaveClass('dropzone-active')
    await fireEvent.dragLeave(zone, { dataTransfer: { files: [] } })
    expect(zone).not.toHaveClass('dropzone-active')
  })

  it('lets the user remove the chosen file', async () => {
    const { user, input } = await setup()
    await user.upload(input, makeFile('pdf-forms.zip'))
    await user.click(await screen.findByRole('button', { name: 'Remove' }))
    expect(screen.queryByText('pdf-forms.zip')).not.toBeInTheDocument()
    expect(submitButton()).toBeDisabled()
  })
})

describe('Upload: sending', () => {
  it('posts the file with the chosen version and the publish switch', async () => {
    const { user, input } = await setup()
    await user.upload(input, makeFile('pdf-forms.zip'))
    await user.type(screen.getByLabelText(/Version/), '1.2.0')
    await user.click(screen.getByRole('switch', { name: /Publish immediately/ }))
    await user.click(submitButton())
    const xhr = FakeXHR.instances[0]
    expect(xhr.url).toBe('/api/v1/packages')
    expect(xhr.sent.get('file').name).toBe('pdf-forms.zip')
    expect(xhr.sent.get('version')).toBe('1.2.0')
    expect(xhr.sent.get('publish')).toBe('true')
    expect(xhr.headers.Authorization).toBe('Bearer jwt-session-token')
  })

  it('saves a draft unless the switch is on', async () => {
    const { user, input } = await setup()
    await user.upload(input, makeFile())
    const toggle = screen.getByRole('switch', { name: /Publish immediately/ })
    expect(toggle).not.toBeChecked()
    expect(screen.getByText(/saved as a draft/)).toBeInTheDocument()
    await user.click(submitButton())
    const xhr = FakeXHR.instances[0]
    expect(xhr.sent.get('publish')).toBe('false')
    expect(xhr.sent.has('version')).toBe(false)
  })

  it('shows real progress, then the processing phase, then the result', async () => {
    const { user, input } = await setup()
    await user.upload(input, makeFile('pdf-forms.zip', 200))
    await user.click(submitButton())
    const xhr = FakeXHR.instances[0]

    const bar = await screen.findByRole('progressbar', { name: 'Upload progress' })
    expect(bar).toHaveAttribute('value', '0')
    expect(submitButton()).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Cancel upload' })).toBeEnabled()

    xhr.progress(100, 200)
    await waitFor(() => expect(bar).toHaveAttribute('value', '50'))
    expect(screen.getByText(/50% .* 100 B of 200 B/)).toBeInTheDocument()

    xhr.progress(200, 200)
    xhr.uploaded()
    const processing = await screen.findByRole('progressbar', {
      name: 'The registry is checking the package',
    })
    expect(processing).not.toHaveAttribute('value')
    expect(screen.getByText(/All bytes sent/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cancel upload' })).not.toBeInTheDocument()

    xhr.respond(201, {
      skill: entrySummary({ name: 'pdf-forms', display_name: 'PDF Forms' }),
      version: versionSummary({ version: '1.2.0', status: 'draft', sha256: 'b'.repeat(64) }),
    })
    expect(await screen.findByText('Package uploaded')).toBeInTheDocument()
    const link = screen.getByRole('link', { name: 'Open the entry' })
    expect(link).toHaveAttribute('href', '#/library/pdf-forms?tab=versions')
    expect(screen.getByRole('link', { name: 'PDF Forms' })).toHaveAttribute('href', '#/library/pdf-forms')
    expect(screen.getByText('b'.repeat(64))).toBeInTheDocument()
    expect(screen.getByText(/The version is a draft/)).toBeInTheDocument()
    expect(toasts.items.some((toast) => toast.message === 'Uploaded pdf-forms 1.2.0.')).toBe(true)
    await waitFor(() => expect(link.closest('section')).toHaveFocus())
  })

  it('does not suggest publishing when the version was published', async () => {
    const { user, input } = await setup()
    await user.upload(input, makeFile())
    await user.click(submitButton())
    FakeXHR.instances[0].respond(
      201,
      uploadResult({ version: versionSummary({ version: '1.2.0', status: 'published' }) }),
    )
    await screen.findByText('Package uploaded')
    expect(screen.queryByText(/The version is a draft/)).not.toBeInTheDocument()
  })

  it('lets the user start over after an upload', async () => {
    const { user, input } = await setup()
    await user.upload(input, makeFile())
    await user.click(submitButton())
    FakeXHR.instances[0].respond(201, uploadResult())
    await user.click(await screen.findByRole('button', { name: 'Upload another package' }))
    expect(screen.getByText(/Drag a \.zip here/)).toBeInTheDocument()
    expect(submitButton()).toBeDisabled()
  })

  it('can cancel a running upload', async () => {
    const { user, input } = await setup()
    await user.upload(input, makeFile())
    await user.click(submitButton())
    await user.click(await screen.findByRole('button', { name: 'Cancel upload' }))
    expect(FakeXHR.instances[0].aborted).toBe(true)
    await waitFor(() => expect(screen.queryByRole('progressbar')).not.toBeInTheDocument())
    expect(submitButton()).toBeEnabled()
    expect(toasts.items.some((toast) => /cancelled/.test(toast.message))).toBe(true)
  })
})

describe('Upload: server errors', () => {
  const REASONS = Object.keys(PACKAGE_REASONS)

  it.each(REASONS)('explains the reason %s in human terms', async (reason) => {
    const { user, input } = await setup()
    await user.upload(input, makeFile())
    await user.click(submitButton())
    FakeXHR.instances[0].respond(422, {
      error: { code: 'INVALID_PACKAGE', message: `Server detail for ${reason}`, reason },
    })
    const alert = await screen.findByRole('alert', {}, { timeout: 2000 })
    const expected = PACKAGE_REASONS[reason]
    expect(within(alert).getByText(expected.title)).toBeInTheDocument()
    expect(alert).toHaveTextContent(expected.explanation)
    expect(alert).toHaveTextContent(expected.fix)
    expect(alert).toHaveTextContent(`Server detail for ${reason}`)
    expect(alert).toHaveTextContent(reason)
    expect(submitButton()).toBeEnabled()
  })

  it('shows conflicts with advice about immutable versions', async () => {
    const { user, input } = await setup()
    await user.upload(input, makeFile())
    await user.click(submitButton())
    FakeXHR.instances[0].respond(409, {
      error: { code: 'CONFLICT', message: "Version 1.2.0 of 'pdf-forms' already exists" },
    })
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent("Version 1.2.0 of 'pdf-forms' already exists")
    expect(alert).toHaveTextContent(/immutable/)
  })

  it('explains a rejected upload that is too large', async () => {
    const { user, input } = await setup()
    await user.upload(input, makeFile())
    await user.click(submitButton())
    FakeXHR.instances[0].respond(413, { error: { code: 'PAYLOAD_TOO_LARGE', message: 'too big' } })
    expect(await screen.findByRole('alert')).toHaveTextContent(/larger than the registry accepts \(25 MiB\)/)
  })

  it('explains a lost connection', async () => {
    const { user, input } = await setup()
    await user.upload(input, makeFile())
    await user.click(submitButton())
    FakeXHR.instances[0].fail()
    expect(await screen.findByRole('alert')).toHaveTextContent(/interrupted/)
  })

  it('lets the user retry after a failure', async () => {
    const { user, input } = await setup()
    await user.upload(input, makeFile())
    await user.click(submitButton())
    FakeXHR.instances[0].respond(422, {
      error: { code: 'INVALID_PACKAGE', message: 'x', reason: 'NOT_A_ZIP' },
    })
    await screen.findByRole('alert')
    await user.click(submitButton())
    expect(FakeXHR.instances).toHaveLength(2)
    FakeXHR.instances[1].respond(201, uploadResult())
    expect(await screen.findByText('Package uploaded')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  describe('bringing the failure into view', () => {
    // The explanation is drawn above the form and the button is below it. After a submit the page can
    // be scrolled so that the explanation is above the window, so the page has to go and get it.
    const FAILURES = [
      [
        'an invalid package',
        (xhr) => xhr.respond(422, { error: { code: 'INVALID_PACKAGE', message: 'x', reason: 'MISSING_ENTRYPOINT' } }),
      ],
      ['a version that exists', (xhr) => xhr.respond(409, { error: { code: 'CONFLICT', message: 'It exists' } })],
      ['an upload that is too large', (xhr) => xhr.respond(413, { error: { code: 'PAYLOAD_TOO_LARGE', message: 'big' } })],
      ['a lost connection', (xhr) => xhr.fail()],
    ]

    it.each(FAILURES)('moves the focus to the explanation and scrolls it into view after %s', async (_name, fail) => {
      const { user, input } = await setup()
      await user.upload(input, makeFile())
      await user.click(submitButton())
      expect(scrolledIntoView()).toEqual([])
      fail(FakeXHR.instances[0])
      const alert = await screen.findByRole('alert')
      await waitFor(() => expect(alert).toHaveFocus())
      expect(alert).toHaveAttribute('id', 'upload-failure')
      expect(scrolledIntoView()).toEqual([{ element: alert, options: { block: 'center', inline: 'nearest' } }])
    })

    it('does it again when a second attempt fails too', async () => {
      const { user, input } = await setup()
      await user.upload(input, makeFile())
      await user.click(submitButton())
      FakeXHR.instances[0].fail()
      await waitFor(() => expect(screen.getByRole('alert')).toHaveFocus())
      await user.click(submitButton())
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      FakeXHR.instances[1].respond(409, { error: { code: 'CONFLICT', message: 'It exists' } })
      const alert = await screen.findByRole('alert')
      await waitFor(() => expect(alert).toHaveFocus())
      expect(scrolledIntoView().map((item) => item.element)).toEqual([expect.anything(), alert])
    })

    it('leaves the focus and the scroll position alone when the user cancels', async () => {
      const { user, input } = await setup()
      await user.upload(input, makeFile())
      await user.click(submitButton())
      await user.click(await screen.findByRole('button', { name: 'Cancel upload' }))
      await waitFor(() => expect(submitButton()).toBeEnabled())
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(scrolledIntoView()).toEqual([])
    })

    it('does not scroll to anything when the upload works', async () => {
      const { user, input } = await setup()
      await user.upload(input, makeFile())
      await user.click(submitButton())
      FakeXHR.instances[0].respond(201, uploadResult())
      await screen.findByText('Package uploaded')
      expect(scrolledIntoView()).toEqual([])
    })
  })

  it('shows server text as text, never as markup', async () => {
    const { user, input, view } = await setup()
    await user.upload(input, makeFile())
    await user.click(submitButton())
    FakeXHR.instances[0].respond(422, {
      error: {
        code: 'INVALID_PACKAGE',
        message: '<img src=x onerror="window.__pwned=1"><b>bold</b>',
        reason: 'BAD_FRONTMATTER',
      },
    })
    await screen.findByRole('alert')
    expect(view.container.querySelector('img')).toBeNull()
    expect(view.container.querySelector('b')).toBeNull()
    expect(screen.getByText(/<img src=x onerror=/)).toBeInTheDocument()
    expect(window.__pwned).toBeUndefined()
  })
})
