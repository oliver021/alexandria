import { render, screen, within } from '@testing-library/svelte'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import FileTreeHarness from '../../test/FileTreeHarness.svelte'

function entry(path, size = 100) {
  return { path, size, sha256: 'a'.repeat(64), media_type: 'text/plain', text: true }
}

const ENTRIES = [
  entry('SKILL.md'),
  entry('scripts/run.py'),
  entry('scripts/util/helpers.py'),
  entry('references/api.md'),
]

function setup(props = {}) {
  const onselect = vi.fn()
  render(FileTreeHarness, { props: { entries: ENTRIES, onselect, open: ['scripts', 'references'], ...props } })
  return { onselect, user: userEvent.setup() }
}

const names = () => screen.getAllByRole('treeitem').map((item) => item.dataset.path)

describe('FileTree', () => {
  it('is a labelled tree with the ARIA metadata of every row', () => {
    setup()
    expect(screen.getByRole('tree', { name: 'Test files' })).toBeInTheDocument()
    expect(names()).toEqual([
      'references',
      'references/api.md',
      'scripts',
      'scripts/util',
      'scripts/run.py',
      'SKILL.md',
    ])
    const scripts = screen.getByRole('treeitem', { name: /scripts/ })
    expect(scripts).toHaveAttribute('aria-expanded', 'true')
    expect(scripts).toHaveAttribute('aria-level', '1')
    expect(scripts).toHaveAttribute('aria-setsize', '3')
    expect(scripts).toHaveAttribute('aria-posinset', '2')
    const run = screen.getByRole('treeitem', { name: /run\.py/ })
    expect(run).toHaveAttribute('aria-level', '2')
    expect(run).not.toHaveAttribute('aria-expanded')
    const util = screen.getByRole('treeitem', { name: /util/ })
    expect(util).toHaveAttribute('aria-expanded', 'false')
  })

  it('has a single tab stop', () => {
    setup()
    const stops = screen.getAllByRole('treeitem').filter((item) => item.tabIndex === 0)
    expect(stops).toHaveLength(1)
  })

  it('selects a file on click and toggles a folder', async () => {
    const { onselect, user } = setup()
    await user.click(screen.getByRole('treeitem', { name: /SKILL\.md/ }))
    expect(onselect).toHaveBeenCalledWith('SKILL.md')
    expect(screen.getByRole('treeitem', { name: /SKILL\.md/ })).toHaveAttribute('aria-selected', 'true')
    await user.click(screen.getByRole('treeitem', { name: /references/ }))
    expect(names()).not.toContain('references/api.md')
    await user.click(screen.getByRole('treeitem', { name: /references/ }))
    expect(names()).toContain('references/api.md')
  })

  it('moves with the arrow keys, Home and End', async () => {
    const { user } = setup()
    screen.getAllByRole('treeitem')[0].focus()
    await user.keyboard('{ArrowDown}')
    expect(screen.getByRole('treeitem', { name: /api\.md/ })).toHaveFocus()
    await user.keyboard('{End}')
    expect(screen.getByRole('treeitem', { name: /SKILL\.md/ })).toHaveFocus()
    await user.keyboard('{ArrowUp}')
    expect(screen.getByRole('treeitem', { name: /run\.py/ })).toHaveFocus()
    await user.keyboard('{Home}')
    expect(screen.getAllByRole('treeitem')[0]).toHaveFocus()
  })

  it('expands, enters and collapses folders with Right and Left', async () => {
    const { user } = setup()
    screen.getByRole('treeitem', { name: /util/ }).focus()
    await user.keyboard('{ArrowRight}')
    expect(names()).toContain('scripts/util/helpers.py')
    expect(screen.getByRole('treeitem', { name: /util/ })).toHaveAttribute('aria-expanded', 'true')
    await user.keyboard('{ArrowRight}')
    expect(screen.getByRole('treeitem', { name: /helpers\.py/ })).toHaveFocus()
    await user.keyboard('{ArrowLeft}')
    expect(screen.getByRole('treeitem', { name: /util/ })).toHaveFocus()
    await user.keyboard('{ArrowLeft}')
    expect(names()).not.toContain('scripts/util/helpers.py')
    await user.keyboard('{ArrowLeft}')
    expect(screen.getByRole('treeitem', { name: /scripts/ })).toHaveFocus()
  })

  it('activates the focused row with Enter and Space', async () => {
    const { onselect, user } = setup()
    screen.getByRole('treeitem', { name: /run\.py/ }).focus()
    await user.keyboard('{Enter}')
    expect(onselect).toHaveBeenLastCalledWith('scripts/run.py')
    screen.getByRole('treeitem', { name: /SKILL\.md/ }).focus()
    await user.keyboard(' ')
    expect(onselect).toHaveBeenLastCalledWith('SKILL.md')
    screen.getByRole('treeitem', { name: /references/ }).focus()
    await user.keyboard('{Enter}')
    expect(names()).not.toContain('references/api.md')
  })

  it('shows file names as text even when they look like markup', () => {
    setup({
      entries: [entry('<img src=x onerror=alert(1)>.md')],
      open: [],
    })
    const item = screen.getByRole('treeitem')
    expect(within(item).getByText('<img src=x onerror=alert(1)>.md')).toBeInTheDocument()
    expect(item.querySelector('img')).toBeNull()
  })
})
