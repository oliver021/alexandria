import { render, screen } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { resetApp } from '../../test/helpers.js'
import { toasts } from '../toast.svelte.js'
import CopyButton from './CopyButton.svelte'

beforeEach(resetApp)
afterEach(() => {
  resetApp()
  delete document.execCommand
})

describe('CopyButton', () => {
  it('copies the text and confirms it', async () => {
    const user = userEvent.setup()
    const writeText = vi.spyOn(navigator.clipboard, 'writeText')
    render(CopyButton, { props: { text: 'value-to-copy', subject: 'the value' } })
    await user.click(screen.getByRole('button', { name: 'Copy the value' }))
    expect(writeText).toHaveBeenCalledWith('value-to-copy')
    expect(await navigator.clipboard.readText()).toBe('value-to-copy')
    expect(await screen.findByRole('button', { name: 'Copied the value' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Copied to the clipboard')
  })

  it('goes back to its label after a moment', async () => {
    const user = userEvent.setup()
    render(CopyButton, { props: { text: 'x' } })
    await user.click(screen.getByRole('button', { name: 'Copy' }))
    await screen.findByRole('button', { name: 'Copied' })
    await vi.waitFor(
      () => expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument(),
      { timeout: 3000 },
    )
  })

  it('tells the user when copying is impossible', async () => {
    const user = userEvent.setup()
    vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(
      new DOMException('denied', 'NotAllowedError'),
    )
    document.execCommand = vi.fn(() => false)
    render(CopyButton, { props: { text: 'x' } })
    await user.click(screen.getByRole('button', { name: 'Copy' }))
    await vi.waitFor(() => expect(toasts.items).toHaveLength(1))
    expect(toasts.items[0]).toMatchObject({ tone: 'error' })
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument()
  })
})
