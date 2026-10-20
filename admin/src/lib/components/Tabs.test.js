import { render, screen } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import Tabs from './Tabs.svelte'

const TABS = [
  { id: 'summary', label: 'Summary' },
  { id: 'versions', label: 'Versions' },
  { id: 'files', label: 'Files' },
]

function setup(selected = 'summary') {
  const onselect = vi.fn()
  render(Tabs, { props: { tabs: TABS, selected, onselect, label: 'Sections', idPrefix: 't' } })
  return { onselect, user: userEvent.setup() }
}

describe('Tabs', () => {
  it('exposes the tab pattern', () => {
    setup('versions')
    expect(screen.getByRole('tablist', { name: 'Sections' })).toBeInTheDocument()
    const tabs = screen.getAllByRole('tab')
    expect(tabs.map((tab) => tab.getAttribute('aria-selected'))).toEqual(['false', 'true', 'false'])
    expect(tabs.map((tab) => tab.getAttribute('aria-controls'))).toEqual([
      't-panel-summary',
      't-panel-versions',
      't-panel-files',
    ])
    expect(tabs.map((tab) => tab.getAttribute('tabindex'))).toEqual(['-1', '0', '-1'])
  })

  it('selects a tab on click', async () => {
    const { onselect, user } = setup()
    await user.click(screen.getByRole('tab', { name: 'Files' }))
    expect(onselect).toHaveBeenCalledWith('files')
  })

  it('moves with the arrow keys, wrapping around, and with Home and End', async () => {
    const { onselect, user } = setup('summary')
    const first = screen.getByRole('tab', { name: 'Summary' })
    first.focus()
    await user.keyboard('{ArrowRight}')
    expect(onselect).toHaveBeenLastCalledWith('versions')
    expect(screen.getByRole('tab', { name: 'Versions' })).toHaveFocus()
    await user.keyboard('{End}')
    expect(onselect).toHaveBeenLastCalledWith('files')
    await user.keyboard('{ArrowRight}')
    expect(onselect).toHaveBeenLastCalledWith('summary')
    await user.keyboard('{ArrowLeft}')
    expect(onselect).toHaveBeenLastCalledWith('files')
    await user.keyboard('{Home}')
    expect(onselect).toHaveBeenLastCalledWith('summary')
  })
})
