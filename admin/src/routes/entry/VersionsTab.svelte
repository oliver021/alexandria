<script>
  import { IntegrityError, downloadPackageVersion } from '../../lib/actions.js'
  import { api, isApiError } from '../../lib/api.js'
  import Alert from '../../lib/components/Alert.svelte'
  import Badge from '../../lib/components/Badge.svelte'
  import ConfirmDialog from '../../lib/components/ConfirmDialog.svelte'
  import CopyButton from '../../lib/components/CopyButton.svelte'
  import EmptyState from '../../lib/components/EmptyState.svelte'
  import Icon from '../../lib/components/Icon.svelte'
  import StatusBadge from '../../lib/components/StatusBadge.svelte'
  import Time from '../../lib/components/Time.svelte'
  import { formatBytes, formatNumber, shortSha } from '../../lib/format.js'
  import { describeError } from '../../lib/reasons.js'
  import { focusAndReveal, focusById } from '../../lib/focus.js'
  import { entryHref } from '../../lib/routes.js'
  import { toasts } from '../../lib/toast.svelte.js'

  /** @type {{ entry: import('../../lib/types.js').EntryDetail, canManage: boolean, onrefresh: () => Promise<unknown>, onselecttab: (id: string) => void }} */
  let { entry, canManage, onrefresh, onselecttab } = $props()

  /** @typedef {{ action: 'publish' | 'deprecate', version: string }} Pending */
  /** @typedef {{ tone: 'warning' | 'danger', title: string, text: string, detail?: string, summaryLink?: boolean }} Notice */
  let pending = $state(/** @type {Pending | null} */ (null))
  let busy = $state(false)
  let notice = $state(/** @type {Notice | null} */ (null))
  let downloading = $state('')

  const publishedCount = $derived(
    entry.versions.filter((/** @type {{ status: string }} */ item) => item.status === 'published')
      .length,
  )
  const pendingIsLastPublished = $derived(
    pending?.action === 'deprecate' &&
      publishedCount === 1 &&
      entry.versions.some(
        (/** @type {{ version: string, status: string }} */ item) =>
          item.version === pending?.version && item.status === 'published',
      ),
  )

  /**
   * @param {'publish' | 'deprecate'} action
   * @param {string} version
   */
  function ask(action, version) {
    notice = null
    pending = { action, version }
  }

  function cancel() {
    if (!busy) pending = null
  }

  async function confirm() {
    if (!pending) return
    const { action, version } = pending
    busy = true
    notice = null
    try {
      if (action === 'publish') await api.publishVersion(entry.name, version)
      else await api.deprecateVersion(entry.name, version)
      pending = null
      toasts.success(
        action === 'publish'
          ? `Version ${version} is published.`
          : `Version ${version} is deprecated.`,
      )
      await onrefresh()
      await focusById('versions-region')
    } catch (error) {
      pending = null
      if (action === 'deprecate' && isApiError(error) && error.status === 409) {
        notice = {
          tone: 'warning',
          title: 'This is the only published version',
          text: 'An entry must keep one published version. Publish another version first, or hide the whole entry from the Summary tab.',
          detail: error.message,
          summaryLink: true,
        }
      } else {
        notice = {
          tone: 'danger',
          title: action === 'publish' ? 'The version was not published' : 'The version was not deprecated',
          text: describeError(error).message,
        }
      }
    } finally {
      busy = false
    }
    // The notice is drawn above the table, and the button that caused it is in one of its rows.
    if (notice) await focusAndReveal('versions-notice')
  }

  /**
   * @param {string} version
   */
  async function download(version) {
    downloading = version
    try {
      const verdict = await downloadPackageVersion(entry.name, version)
      const filename = `${entry.name}-${version}.zip`
      if (verdict === 'match') toasts.success(`Downloaded ${filename}. Its SHA-256 matches.`)
      else toasts.warning(`Downloaded ${filename}, but this browser could not verify its SHA-256.`)
    } catch (error) {
      toasts.error(
        error instanceof IntegrityError ? error.message : describeError(error).message,
      )
    } finally {
      downloading = ''
    }
  }
</script>

<div class="stack" id="versions-region" tabindex="-1">
  {#if notice}
    <Alert tone={notice.tone} title={notice.title} id="versions-notice" focusable>
      <p>{notice.text}</p>
      {#if notice.detail}<p class="small">Server message: {notice.detail}</p>{/if}
      {#snippet actions()}
        {#if notice?.summaryLink}
          <button type="button" class="btn btn-sm" onclick={() => onselecttab('summary')}>
            Go to Summary
          </button>
        {/if}
        <button type="button" class="btn btn-sm btn-quiet" onclick={() => (notice = null)}>
          Dismiss
        </button>
      {/snippet}
    </Alert>
  {/if}

  {#if entry.versions.length === 0}
    <div class="card">
      <EmptyState
        title="No versions you can see"
        text="Draft versions are visible only to the owner and admins."
      />
    </div>
  {:else}
    <div class="table-wrap">
      <table class="table">
        <caption class="visually-hidden">Versions of {entry.display_name}, newest first</caption>
        <thead>
          <tr>
            <th scope="col">Version</th>
            <th scope="col">Status</th>
            <th scope="col" class="hide-narrow">Uploaded</th>
            <th scope="col" class="num hide-narrow">Size</th>
            <th scope="col" class="num hide-narrow">Files</th>
            <th scope="col" class="hide-narrow">SHA-256</th>
            <th scope="col"><span class="visually-hidden">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {#each entry.versions as item (item.id)}
            <tr class={[item.status === 'deprecated' && 'row-muted']}>
              <th scope="row">
                <span class="mono">{item.version}</span>
                {#if item.version === entry.latest_version}
                  <Badge tone="ok">latest</Badge>
                {/if}
              </th>
              <td>
                <div class="stack-tight">
                  <StatusBadge status={item.status} />
                  <span class="small muted">
                    {#if item.status === 'published'}
                      Published <Time value={item.published_at} />
                    {:else if item.status === 'deprecated'}
                      Deprecated <Time value={item.deprecated_at} />
                    {:else}
                      Not published
                    {/if}
                  </span>
                </div>
              </td>
              <td class="nowrap hide-narrow"><Time value={item.created_at} /></td>
              <td class="num nowrap hide-narrow">{formatBytes(item.size_bytes)}</td>
              <td class="num hide-narrow">{formatNumber(item.file_count)}</td>
              <td class="hide-narrow">
                <div class="cluster">
                  <code title={item.sha256}>{shortSha(item.sha256)}</code>
                  <CopyButton text={item.sha256} subject="SHA-256 of version {item.version}" />
                </div>
              </td>
              <td class="actions">
                <div class="row-actions">
                  <a
                    class="btn btn-sm"
                    href={entryHref(entry.name, { tab: 'files', version: item.version })}
                  >
                    <Icon name="folder" size={14} />
                    Files <span class="visually-hidden">of version {item.version}</span>
                  </a>
                  <button
                    type="button"
                    class="btn btn-sm"
                    disabled={downloading !== ''}
                    onclick={() => download(item.version)}
                  >
                    {#if downloading === item.version}
                      <span class="spinner" aria-hidden="true"></span>
                    {:else}
                      <Icon name="download" size={14} />
                    {/if}
                    Download <span class="visually-hidden">version {item.version}</span>
                  </button>
                  {#if canManage && item.status !== 'published'}
                    <button
                      type="button"
                      class="btn btn-sm btn-primary"
                      onclick={() => ask('publish', item.version)}
                    >
                      {item.status === 'deprecated' ? 'Publish again' : 'Publish'}
                      <span class="visually-hidden">version {item.version}</span>
                    </button>
                  {/if}
                  {#if canManage && item.status === 'published'}
                    <button
                      type="button"
                      class="btn btn-sm btn-outline-danger"
                      onclick={() => ask('deprecate', item.version)}
                    >
                      Deprecate <span class="visually-hidden">version {item.version}</span>
                    </button>
                  {/if}
                </div>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    {#if !canManage}
      <p class="muted small">
        Only the owner of this entry and admins can publish or deprecate versions.
      </p>
    {/if}
  {/if}
</div>

<ConfirmDialog
  open={pending?.action === 'publish'}
  title="Publish version {pending?.version}?"
  confirmLabel="Publish version"
  {busy}
  onconfirm={confirm}
  oncancel={cancel}
>
  <p>
    Anyone who can read this registry will be able to find and download version
    <strong class="mono">{pending?.version}</strong>. A published version can become
    <code>latest</code>, and its content never changes afterwards.
  </p>
</ConfirmDialog>

<ConfirmDialog
  open={pending?.action === 'deprecate'}
  title="Deprecate version {pending?.version}?"
  confirmLabel="Deprecate version"
  tone="danger"
  {busy}
  onconfirm={confirm}
  oncancel={cancel}
>
  <p>
    Version <strong class="mono">{pending?.version}</strong> stays downloadable by its exact
    number, but it will no longer be <code>latest</code> and will be marked as deprecated. You can
    publish it again later.
  </p>
  {#if pendingIsLastPublished}
    <Alert tone="warning" live={false}>
      This is the only published version, so the registry will refuse. Publish another version
      first, or hide the entry instead.
    </Alert>
  {/if}
</ConfirmDialog>
