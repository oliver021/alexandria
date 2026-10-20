<script>
  import { untrack } from 'svelte'
  import { buildTree, initialExpanded } from '../lib/filetree.js'
  import FileTree from '../lib/components/FileTree.svelte'

  /** @type {{ entries: import('../lib/types.js').ManifestEntry[], onselect?: (path: string) => void, open?: string[] }} */
  let { entries, onselect, open = [] } = $props()

  const root = $derived(buildTree(entries))
  let expanded = $state(
    untrack(() => new Set(open.length > 0 ? open : initialExpanded(buildTree(entries), null, 0))),
  )
  let selected = $state(/** @type {string | null} */ (null))

  /**
   * @param {string} path
   */
  function toggle(path) {
    const next = new Set(expanded)
    if (next.has(path)) next.delete(path)
    else next.add(path)
    expanded = next
  }

  /**
   * @param {string} path
   */
  function select(path) {
    selected = path
    onselect?.(path)
  }
</script>

<FileTree {root} {expanded} {selected} ontoggle={toggle} onselect={select} label="Test files" />
