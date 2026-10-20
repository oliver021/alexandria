import { render, screen } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import Pagination from './Pagination.svelte'

function setup(overrides = {}) {
  const onpage = vi.fn()
  render(Pagination, {
    props: { page: 2, pageCount: 7, total: 133, pageSize: 20, onpage, ...overrides },
  })
  return { onpage, user: userEvent.setup() }
}

describe('Pagination', () => {
  it('summarizes the range being shown', () => {
    setup()
    expect(screen.getByText(/Showing 21.40 of 133/)).toBeInTheDocument()
  })

  it('marks the current page', () => {
    setup()
    expect(screen.getByRole('button', { name: 'Page 2' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('button', { name: 'Page 1' })).not.toHaveAttribute('aria-current')
  })

  it('navigates with the numbers and the previous/next buttons', async () => {
    const { onpage, user } = setup()
    await user.click(screen.getByRole('button', { name: 'Next' }))
    expect(onpage).toHaveBeenLastCalledWith(3)
    await user.click(screen.getByRole('button', { name: 'Previous' }))
    expect(onpage).toHaveBeenLastCalledWith(1)
    await user.click(screen.getByRole('button', { name: 'Page 7' }))
    expect(onpage).toHaveBeenLastCalledWith(7)
  })

  it('disables the edges', () => {
    setup({ page: 1 })
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled()
  })

  it('shows only the summary for a single page', () => {
    setup({ page: 1, pageCount: 1, total: 5 })
    expect(screen.getByText(/Showing 1.5 of 5/)).toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: 'Pagination' })).not.toBeInTheDocument()
  })

  it('renders nothing without results', () => {
    setup({ total: 0, pageCount: 1, page: 1 })
    expect(screen.queryByText(/Showing/)).not.toBeInTheDocument()
  })
})
