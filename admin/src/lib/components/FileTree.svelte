<script>
  import { tick } from 'svelte'
  import { flattenTree } from '../filetree.js'
  import { formatBytes, pluralize } from '../format.js'
  import Icon from './Icon.svelte'

  /** @type {{ root: import('../filetree.js').DirNode, expanded: Set<string>, selected?: string | null, onselect: (path: string) => void, ontoggle: (path: string) => void, label?: string }} */
  let { root, expanded, selected = null, onselect, ontoggle, label = 'Package files' } = $props()

  /** @type {HTMLElement | undefined} */
  let container = $state()
  /** @type {string | null} */
  let focusedPath = $state(null)

  const rows = $derived(flattenTree(root, expanded))
  const tabStop = $derived.by(() => {
    if (focusedPath !== null && rows.some((row) => row.node.path === focusedPath)) return focusedPath
    if (selected !== null && rows.some((row) => row.node.path === selected)) return selected
    return rows[0]?.node.path ?? null
  })

  /**
   * @param {string} path
   */
  async function focusPath(path) {
    focusedPath = path
    await tick()
    const items = container?.querySelectorAll('[role="treeitem"]') ?? []
    for (const item of items) {
      if (item instanceof HTMLElement && item.dataset.path === path) {
        item.focus()
        return
      }
    }
  }

  /**
   * @param {import('../filetree.js').TreeNode} node
   */
  function activate(node) {
    focusedPath = node.path
    if (node.type === 'dir') ontoggle(node.path)
    else onselect(node.path)
  }

  /**
   * @param {KeyboardEvent} event
   * @param {number} index
   */
  function onkeydown(event, index) {
    const row = rows[index]
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault()
        if (index + 1 < rows.length) focusPath(rows[index + 1].node.path)
        break
      case 'ArrowUp':
        event.preventDefault()
        if (index > 0) focusPath(rows[index - 1].node.path)
        break
      case 'Home':
        event.preventDefault()
        focusPath(rows[0].node.path)
        break
      case 'End':
        event.preventDefault()
        focusPath(rows[rows.length - 1].node.path)
        break
      case 'ArrowRight':
        event.preventDefault()
        if (row.node.type === 'dir') {
          if (!row.expanded) ontoggle(row.node.path)
          else if (index + 1 < rows.length) focusPath(rows[index + 1].node.path)
        }
        break
      case 'ArrowLeft':
        event.preventDefault()
        if (row.node.type === 'dir' && row.expanded) ontoggle(row.node.path)
        else if (row.parentPath !== '') focusPath(row.parentPath)
        break
      case 'Enter':
      case ' ':
        event.preventDefault()
        activate(row.node)
        break
      default:
        break
    }
  }
</script>

<ul class="tree" role="tree" aria-label={label} bind:this={container}>
  {#each rows as row, index (row.node.path)}
    <li
      class="tree__item"
      role="treeitem"
      data-path={row.node.path}
      aria-level={row.depth + 1}
      aria-setsize={row.setSize}
      aria-posinset={row.position}
      aria-expanded={row.node.type === 'dir' ? row.expanded : undefined}
      aria-selected={row.node.type === 'file' ? row.node.path === selected : undefined}
      tabindex={row.node.path === tabStop ? 0 : -1}
      style:--depth={row.depth}
      onclick={() => activate(row.node)}
      onkeydown={(event) => onkeydown(event, index)}
    >
      {#if row.node.type === 'dir'}
        <span class={['tree__chevron', row.expanded && 'tree__chevron-open']}>
          <Icon name="chevron-right" size={16} />
        </span>
        <Icon name="folder" size={16} />
      {:else}
        <span class="tree__chevron"></span>
        <Icon name="file" size={16} />
      {/if}
      <span class="tree__label" title={row.node.path}>{row.node.name}</span>
      <span class="tree__meta">
        {#if row.node.type === 'dir'}
          {pluralize(row.node.fileCount, 'file')}
        {:else}
          {formatBytes(row.node.size)}
        {/if}
      </span>
    </li>
  {/each}
</ul>
