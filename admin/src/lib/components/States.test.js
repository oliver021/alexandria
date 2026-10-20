import { render, screen } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api.js'
import EmptyState from './EmptyState.svelte'
import ErrorState from './ErrorState.svelte'
import LoadingState from './LoadingState.svelte'

describe('LoadingState', () => {
  it('announces what is loading', () => {
    render(LoadingState, { props: { label: 'Loading tokens' } })
    expect(screen.getByRole('status')).toHaveTextContent('Loading tokens')
  })
})

describe('EmptyState', () => {
  it('shows its title and text, as a paragraph by default', () => {
    render(EmptyState, { props: { title: 'Nothing here', text: 'Add something.' } })
    expect(screen.getByText('Nothing here').tagName).toBe('P')
    expect(screen.getByText('Add something.')).toBeInTheDocument()
  })

  it('can use a real heading for whole-page states', () => {
    render(EmptyState, { props: { title: 'Page not found', heading: 'h1' } })
    expect(screen.getByRole('heading', { level: 1, name: 'Page not found' })).toBeInTheDocument()
  })
})

describe('ErrorState', () => {
  const error = new ApiError({ status: 500, code: 'INTERNAL_ERROR', message: 'secret detail' })

  it('explains the failure without leaking server internals', () => {
    render(ErrorState, { props: { error } })
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('The registry failed')
    expect(alert).not.toHaveTextContent('secret detail')
  })

  it('offers a retry', async () => {
    const onretry = vi.fn()
    const user = userEvent.setup()
    render(ErrorState, { props: { error, onretry } })
    await user.click(screen.getByRole('button', { name: /Try again/ }))
    expect(onretry).toHaveBeenCalledTimes(1)
  })

  it('accepts a custom title and heading level', () => {
    render(ErrorState, { props: { error, title: 'Custom title', heading: 'h1' } })
    expect(screen.getByRole('heading', { level: 1, name: 'Custom title' })).toBeInTheDocument()
  })

  it('does not offer a retry unless asked', () => {
    render(ErrorState, { props: { error } })
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})
