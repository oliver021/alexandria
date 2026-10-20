import { render, screen } from '@testing-library/svelte'
import { describe, expect, it } from 'vitest'
import Time from './Time.svelte'

describe('Time', () => {
  it('renders a machine-readable date with the full date as a tooltip', () => {
    render(Time, { props: { value: '2026-03-05T23:30:00Z' } })
    const element = screen.getByText(/2026/)
    expect(element.tagName).toBe('TIME')
    expect(element).toHaveAttribute('datetime', '2026-03-05T23:30:00Z')
    expect(element).toHaveAttribute('title')
  })

  it('renders relative times', () => {
    const recent = new Date(Date.now() - 5 * 60_000).toISOString()
    render(Time, { props: { value: recent, mode: 'relative' } })
    expect(screen.getByText('5 minutes ago')).toBeInTheDocument()
  })

  it('renders a dash for a missing value', () => {
    render(Time, { props: { value: null } })
    expect(screen.getByLabelText('none')).toBeInTheDocument()
  })
})
