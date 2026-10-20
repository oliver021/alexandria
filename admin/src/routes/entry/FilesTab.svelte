<script>
  import { untrack } from 'svelte'
  import { api } from '../../lib/api.js'
  import EmptyState from '../../lib/components/EmptyState.svelte'
  import ErrorState from '../../lib/components/ErrorState.svelte'
  import FileTree from '../../lib/components/FileTree.svelte'
  import LoadingState from '../../lib/components/LoadingState.svelte'
  import { buildTree, initialExpanded } from '../../lib/filetree.js'
  import { formatBytes, pluralize } from '../../lib/format.js'
  import { useResource } from '../../lib/resource.svelte.js'
  import FileViewer from './FileViewer.svelte'
  import VersionSelect from './VersionSelect.svelte'

  /** @type {{ entry: import('../../lib/types.js').EntryDetail, version: string | null, file: string | null, inlineLimit?: number | null, onversion: (version: string) => void, onfile: (path: string) => void }} */
  let { entry, version, file, inlineLimit = null, onversion, onfile } = $props()

  const manifest = useResource((signal, name, selected) => api.listFiles(name, selected, { signal }))

  /** @type {Set<string>} */
  let expanded = $state(new Set())

  const files = $derived(manifest.data?.files ?? [])
  const root = $derived(manifest.data ? buildTree(files) : null)
  const selectedFile = $derived(file ? (files.find((item) => item.path === file) ?? null) : null)

  $effect(() => {
    const name = entry.name
    const selected = version
    if (selected) untrack(() => manifest.load(name, selected))
  })

  $effect(() => {
    const tree = root
    if (tree) {
      untrack(() => {
        expanded = initialExpanded(tree, file)
      })
    }
  })

  /**
   * @param {string} path
   */
  function toggle(path) {
    const next = new Set(expanded)
    if (next.has(path)) next.delete(path)
    else next.add(path)
    expanded = next
  }
</script>

<div class="stack">
  <div class="files-bar">
    <VersionSelect
      id="files-version"
      versions={entry.versions}
      value={version}
      latest={entry.latest_version}
      onchange={onversion}
    />
    {#if manifest.data && root}
      <p class="muted small files-bar__summary">
        {pluralize(root.fileCount, 'file')}, {formatBytes(root.size)} unpacked
      </p>
    {/if}
  </div>

  {#if !version}
    <div class="card">
      <EmptyState title="No version to browse" text="This entry has no version you can see." />
    </div>
  {:else if manifest.error}
    <div class="card">
      <ErrorState error={manifest.error} onretry={manifest.reload} />
    </div>
  {:else if !manifest.loaded || !root}
    <div class="card"><LoadingState label="Loading files&hellip;" /></div>
  {:else if files.length === 0}
    <div class="card">
      <EmptyState icon="file" title="This version has no files" />
    </div>
  {:else}
    <div class="files">
      <div class="card files__tree">
        <div class="card__body files__tree-body">
          <FileTree
            {root}
            {expanded}
            selected={file}
            ontoggle={toggle}
            onselect={onfile}
            label="Files of version {version}"
          />
        </div>
      </div>
      <div class="files__viewer">
        {#if selectedFile}
          {#key `${version}:${selectedFile.path}`}
            <FileViewer name={entry.name} {version} file={selectedFile} {inlineLimit} />
          {/key}
        {:else}
          <div class="card">
            <EmptyState
              icon="file"
              title={file ? 'That file is not in this version' : 'Select a file'}
              text={file
                ? 'Pick another file from the tree.'
                : 'Choose a file in the tree to see its contents. Text files are shown as plain text.'}
            />
          </div>
        {/if}
      </div>
    </div>
  {/if}
</div>
