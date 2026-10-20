<script>
  import { onMount, untrack } from 'svelte'
  import { api, isApiError } from '../lib/api.js'
  import { session } from '../lib/auth.svelte.js'
  import ErrorState from '../lib/components/ErrorState.svelte'
  import KindBadge from '../lib/components/KindBadge.svelte'
  import LoadingState from '../lib/components/LoadingState.svelte'
  import StatusBadge from '../lib/components/StatusBadge.svelte'
  import Tabs from '../lib/components/Tabs.svelte'
  import Time from '../lib/components/Time.svelte'
  import { useResource } from '../lib/resource.svelte.js'
  import { router, setPageTitle } from '../lib/router.svelte.js'
  import { hrefFor, paths } from '../lib/routes.js'
  import { sortVersionsDescending } from '../lib/semver.js'
  import FilesTab from './entry/FilesTab.svelte'
  import SummaryTab from './entry/SummaryTab.svelte'
  import UseTab from './entry/UseTab.svelte'
  import VersionsTab from './entry/VersionsTab.svelte'

  const TABS = [
    { id: 'summary', label: 'Summary' },
    { id: 'versions', label: 'Versions' },
    { id: 'files', label: 'Files' },
    { id: 'use', label: 'Use' },
  ]

  const ref = $derived(router.route.params.name)
  const requestedTab = $derived(router.route.query.get('tab'))
  const tab = $derived(TABS.find((item) => item.id === requestedTab)?.id ?? 'summary')
  const requestedVersion = $derived(router.route.query.get('version'))
  const file = $derived(router.route.query.get('file'))

  const entryResource = useResource((signal, name) => api.getEntry(name, { signal }))
  const detailResource = useResource((signal, name, version) =>
    api.getVersion(name, version, { signal }),
  )
  const infoResource = useResource((signal) => api.info({ signal }))
  const usersResource = useResource((signal) => api.listUsers({ signal }))

  const entry = $derived(
    entryResource.data
      ? { ...entryResource.data, versions: sortVersionsDescending(entryResource.data.versions) }
      : undefined,
  )
  const notFound = $derived(isApiError(entryResource.error) && entryResource.error.status === 404)
  const versions = $derived(entry?.versions ?? [])
  const selectedVersion = $derived.by(() => {
    if (!entry) return null
    if (requestedVersion && versions.some((item) => item.version === requestedVersion)) {
      return requestedVersion
    }
    if (entry.latest_version && versions.some((item) => item.version === entry.latest_version)) {
      return entry.latest_version
    }
    return versions[0]?.version ?? null
  })
  const selectedStatus = $derived(
    versions.find((item) => item.version === selectedVersion)?.status ?? '',
  )
  const detail = $derived(
    detailResource.data && detailResource.data.version === selectedVersion
      ? detailResource.data
      : null,
  )
  const canManage = $derived(
    Boolean(entry && session.user && (session.isAdmin || session.user.id === entry.owner_id)),
  )
  const ownerLabel = $derived.by(() => {
    if (!entry) return ''
    if (session.user && session.user.id === entry.owner_id) {
      return `${session.user.username} (you)`
    }
    const owner = usersResource.data?.find((user) => user.id === entry.owner_id)
    return owner ? owner.username : `User ${entry.owner_id.slice(0, 8)}`
  })

  onMount(() => {
    infoResource.load()
    if (session.isAdmin) usersResource.load()
  })

  $effect(() => {
    const name = ref
    untrack(() => entryResource.load(name))
  })

  $effect(() => {
    const name = entry?.name
    const version = selectedVersion
    if (name && version) untrack(() => detailResource.load(name, version))
  })

  $effect(() => {
    if (entry) setPageTitle(entry.display_name)
  })

  /**
   * @param {Record<string, string | null | undefined>} patch
   */
  function go(patch) {
    const query = {
      tab: tab === 'summary' ? null : tab,
      version: requestedVersion,
      file,
      ...patch,
    }
    router.navigate(paths.entry(ref), { query, replace: true })
  }

  /**
   * @param {string} id
   */
  function selectTab(id) {
    go({ tab: id === 'summary' ? null : id, file: id === 'files' ? file : null })
  }

  /**
   * @param {string} version
   */
  function selectVersion(version) {
    go({ version, file: null })
  }

  /**
   * @param {string} path
   */
  function selectFile(path) {
    go({ file: path })
  }

  async function refresh() {
    await entryResource.reload()
  }
</script>

{#if entryResource.error && !entry}
  <div class="card">
    <ErrorState
      error={entryResource.error}
      heading="h1"
      title={notFound ? 'Entry not found' : undefined}
      onretry={entryResource.reload}
    >
      <a class="btn" href={hrefFor(paths.library())}>Back to the library</a>
    </ErrorState>
  </div>
{:else if !entry}
  <LoadingState label="Loading entry&hellip;" />
{:else}
  <nav aria-label="Breadcrumb">
    <ol class="crumbs">
      <li><a href={hrefFor(paths.library())}>Library</a></li>
      <li aria-current="page">{entry.name}</li>
    </ol>
  </nav>

  <div class="entry-head">
    <div class="entry-head__text">
      <h1>{entry.display_name}</h1>
      <p class="mono muted wrap-anywhere">{entry.name}</p>
    </div>
    <div class="entry-head__meta">
      <div class="cluster">
        <KindBadge kind={entry.kind} />
        <StatusBadge status={entry.status} />
      </div>
      <p class="small muted">
        {#if entry.latest_version}
          Latest <span class="mono">{entry.latest_version}</span> &middot;
        {/if}
        Updated <Time value={entry.updated_at} mode="relative" />
      </p>
    </div>
  </div>

  {#if entryResource.error}
    <p class="field-error" role="alert">
      The entry could not be refreshed.
      <button type="button" class="btn btn-sm" onclick={() => entryResource.reload()}>
        Try again
      </button>
    </p>
  {/if}

  <Tabs tabs={TABS} selected={tab} onselect={selectTab} label="Entry sections" idPrefix="entry" />

  <div
    class="tabpanel"
    role="tabpanel"
    id="entry-panel-{tab}"
    aria-labelledby="entry-tab-{tab}"
  >
    {#if tab === 'summary'}
      <SummaryTab
        {entry}
        {detail}
        {canManage}
        {ownerLabel}
        {selectedVersion}
        onrefresh={refresh}
      />
    {:else if tab === 'versions'}
      <VersionsTab {entry} {canManage} onrefresh={refresh} onselecttab={selectTab} />
    {:else if tab === 'files'}
      <FilesTab
        {entry}
        version={selectedVersion}
        {file}
        inlineLimit={infoResource.data?.limits.max_inline_read_bytes ?? null}
        onversion={selectVersion}
        onfile={selectFile}
      />
    {:else}
      <UseTab
        {entry}
        version={selectedVersion}
        versionStatus={selectedStatus}
        info={infoResource.data ?? null}
        {detail}
        onversion={selectVersion}
      />
    {/if}
  </div>
{/if}
