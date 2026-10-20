import { render, screen } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { createRawSnippet } from 'svelte'
import { describe, expect, it, vi } from 'vitest'
import ConfirmDialog from './ConfirmDialog.svelte'

const body = createRawSnippet(() => ({ render: () => '<p>This cannot be undone.</p>' }))

function props(overrides = {}) {
  return {
    open: true,
    title: 'Delete the thing?',
    confirmLabel: 'Delete it',
    children: body,
    onconfirm: vi.fn(),
    oncancel: vi.fn(),
    ...overrides,
  }
}

describe('ConfirmDialog', () => {
  it('renders nothing while closed', () => {
    render(ConfirmDialog, { props: props({ open: false }) })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('is a named, described modal dialog', async () => {
    render(ConfirmDialog, { props: props() })
    const dialog = await screen.findByRole('dialog', { name: 'Delete the thing?' })
    expect(dialog).toHaveAttribute('open')
    expect(dialog).toHaveAccessibleDescription('This cannot be undone.')
  })

  it('puts the initial focus on the safe choice', async () => {
    render(ConfirmDialog, { props: props() })
    const cancel = await screen.findByRole('button', { name: 'Cancel' })
    expect(cancel).toHaveFocus()
  })

  it('calls onconfirm and oncancel from its buttons', async () => {
    const user = userEvent.setup()
    const options = props()
    render(ConfirmDialog, { props: options })
    await user.click(await screen.findByRole('button', { name: 'Delete it' }))
    expect(options.onconfirm).toHaveBeenCalledTimes(1)
    expect(options.oncancel).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(options.oncancel).toHaveBeenCalledTimes(1)
  })

  it('treats the Escape key (the cancel event) as a cancellation', async () => {
    const options = props()
    render(ConfirmDialog, { props: options })
    const dialog = await screen.findByRole('dialog')
    const event = new Event('cancel', { cancelable: true })
    dialog.dispatchEvent(event)
    expect(event.defaultPrevented).toBe(true)
    expect(options.oncancel).toHaveBeenCalledTimes(1)
  })

  it('cannot be dismissed or repeated while busy', async () => {
    const user = userEvent.setup()
    const options = props({ busy: true })
    render(ConfirmDialog, { props: options })
    const confirm = await screen.findByRole('button', { name: 'Delete it' })
    expect(confirm).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled()
    await user.click(confirm)
    screen.getByRole('dialog').dispatchEvent(new Event('cancel', { cancelable: true }))
    expect(options.onconfirm).not.toHaveBeenCalled()
    expect(options.oncancel).not.toHaveBeenCalled()
  })

  it('announces an error inside the dialog', async () => {
    render(ConfirmDialog, { props: props({ error: 'The server said no.' }) })
    expect(await screen.findByRole('alert')).toHaveTextContent('The server said no.')
  })

  it('uses a danger style for destructive actions', async () => {
    render(ConfirmDialog, { props: props({ tone: 'danger' }) })
    expect(await screen.findByRole('button', { name: 'Delete it' })).toHaveClass('btn-danger')
  })

  it('still opens in browsers without showModal', async () => {
    const original = HTMLDialogElement.prototype.showModal
    HTMLDialogElement.prototype.showModal = /** @type {any} */ (undefined)
    try {
      render(ConfirmDialog, { props: props() })
      const dialog = await screen.findByRole('dialog', { name: 'Delete the thing?' })
      expect(dialog).toHaveAttribute('open')
    } finally {
      HTMLDialogElement.prototype.showModal = original
    }
  })

  it('returns the focus to the element that opened it', async () => {
    const opener = document.createElement('button')
    opener.textContent = 'Open'
    document.body.appendChild(opener)
    opener.focus()
    const { rerender } = render(ConfirmDialog, { props: props({ open: false }) })
    await rerender(props({ open: true }))
    expect(await screen.findByRole('button', { name: 'Cancel' })).toHaveFocus()
    await rerender(props({ open: false }))
    await vi.waitFor(() => expect(opener).toHaveFocus())
    opener.remove()
  })
})
