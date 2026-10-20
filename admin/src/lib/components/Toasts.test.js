import { render, screen } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { resetApp } from '../../test/helpers.js'
import { toasts } from '../toast.svelte.js'
import Toasts from './Toasts.svelte'

beforeEach(resetApp)
afterEach(resetApp)

describe('Toasts', () => {
  it('is a polite live region that exists before any message', () => {
    const { container } = render(Toasts)
    const region = container.querySelector('.toasts')
    expect(region).toHaveAttribute('role', 'status')
    expect(region).toHaveAttribute('aria-live', 'polite')
    expect(region).toHaveAttribute('aria-atomic', 'false')
    expect(region?.children).toHaveLength(0)
  })

  it('shows messages as text with a tone class', async () => {
    const { container } = render(Toasts)
    toasts.success('Saved <b>safely</b>')
    toasts.error('It broke')
    expect(await screen.findByText('Saved <b>safely</b>')).toBeInTheDocument()
    expect(container.querySelector('b')).toBeNull()
    expect(container.querySelectorAll('.toast')).toHaveLength(2)
    expect(container.querySelector('.toast-success')).toBeInTheDocument()
    expect(container.querySelector('.toast-error')).toBeInTheDocument()
  })

  it('can be dismissed with the keyboard-accessible button', async () => {
    const user = userEvent.setup()
    render(Toasts)
    toasts.error('Dismiss me')
    await user.click(await screen.findByRole('button', { name: 'Dismiss notification' }))
    expect(screen.queryByText('Dismiss me')).not.toBeInTheDocument()
    expect(toasts.items).toHaveLength(0)
  })
})
