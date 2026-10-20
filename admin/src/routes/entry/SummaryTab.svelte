<script>
  import { api } from '../../lib/api.js'
  import Alert from '../../lib/components/Alert.svelte'
  import ConfirmDialog from '../../lib/components/ConfirmDialog.svelte'
  import KindBadge from '../../lib/components/KindBadge.svelte'
  import StatusBadge from '../../lib/components/StatusBadge.svelte'
  import Time from '../../lib/components/Time.svelte'
  import { focusById } from '../../lib/focus.js'
  import { pluralize } from '../../lib/format.js'
  import { describeError } from '../../lib/reasons.js'
  import { hrefFor, paths } from '../../lib/routes.js'
  import { toasts } from '../../lib/toast.svelte.js'

  const MAX_VALUE_LENGTH = 2000

  /** @type {{ entry: import('../../lib/types.js').EntryDetail, detail: import('../../lib/types.js').VersionDetail | null, canManage: boolean, ownerLabel: string, selectedVersion: string | null, onrefresh: () => Promise<unknown> }} */
  let { entry, detail, canManage, ownerLabel, selectedVersion, onrefresh } = $props()

  let confirming = $state('')
  let busy = $state(false)
  let failure = $state('')

  /** @type {Record<string, string>} */
  const statusNotes = {
    published: 'Visible to everyone who can read this registry.',
    draft: 'No version is published yet, so only the owner and admins can see it.',
    unpublished: 'Hidden by its owner or an admin. Only they can see it.',
  }

  const metadataEntries = $derived(
    Object.entries(detail?.metadata ?? {}).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)),
  )

  /**
   * @param {unknown} value
   * @returns {{ text: string, block: boolean }}
   */
  function renderValue(value) {
    let text
    let block = false
    if (typeof value === 'string') {
      text = value
      block = value.includes('\n')
    } else if (value !== null && typeof value === 'object') {
      text = JSON.stringify(value, null, 2)
      block = true
    } else {
      text = String(value)
    }
    if (text.length > MAX_VALUE_LENGTH) text = `${text.slice(0, MAX_VALUE_LENGTH)}…`
    return { text, block }
  }

  function closeDialog() {
    if (busy) return
    confirming = ''
    failure = ''
  }

  async function hide() {
    busy = true
    failure = ''
    try {
      await api.hideEntry(entry.name)
      confirming = ''
      toasts.success(`${entry.display_name} is now hidden.`)
      await onrefresh()
      await focusById('visibility-action')
    } catch (error) {
      failure = describeError(error).message
    } finally {
      busy = false
    }
  }

  async function restore() {
    busy = true
    failure = ''
    try {
      await api.restoreEntry(entry.name)
      confirming = ''
      toasts.success(`${entry.display_name} is visible again.`)
      await onrefresh()
      await focusById('visibility-action')
    } catch (error) {
      failure = describeError(error).message
    } finally {
      busy = false
    }
  }
</script>

<div class="two-col">
  <div class="stack-lg">
    <section class="card" aria-labelledby="summary-description">
      <div class="card__head">
        <h2 id="summary-description">Description</h2>
      </div>
      <div class="card__body">
        <p class="prose">{entry.description}</p>
      </div>
    </section>

    <section class="card" aria-labelledby="summary-details">
      <div class="card__head">
        <h2 id="summary-details">Details</h2>
      </div>
      <div class="card__body">
        <dl class="dl">
          <dt>Kind</dt>
          <dd>
            <KindBadge kind={entry.kind} />
            <span class="muted small">
              {entry.kind === 'skill'
                ? 'Procedural. The entry point is SKILL.md.'
                : 'Referential. The entry point is INDEX.md.'}
            </span>
          </dd>
          <dt>Status</dt>
          <dd>
            <StatusBadge status={entry.status} />
            <span class="muted small">{statusNotes[entry.status]}</span>
          </dd>
          <dt>Category</dt>
          <dd>
            {#if entry.category}
              <a href={hrefFor(paths.library(), { category: entry.category })}>{entry.category}</a>
            {:else}
              <span class="faint">None</span>
            {/if}
          </dd>
          <dt>Tags</dt>
          <dd>
            {#if entry.tags.length > 0}
              <ul class="chips">
                {#each entry.tags as tag (tag)}
                  <li>
                    <a class="chip" href={hrefFor(paths.library(), { tag })}>{tag}</a>
                  </li>
                {/each}
              </ul>
            {:else}
              <span class="faint">None</span>
            {/if}
          </dd>
          <dt>Latest version</dt>
          <dd>
            {#if entry.latest_version}
              <span class="mono">{entry.latest_version}</span>
            {:else}
              <span class="faint">No published version</span>
            {/if}
            <span class="muted small">of {pluralize(entry.versions.length, 'version')}</span>
          </dd>
          <dt>Owner</dt>
          <dd>{ownerLabel}</dd>
          <dt>Created</dt>
          <dd><Time value={entry.created_at} mode="datetime" /></dd>
          <dt>Updated</dt>
          <dd><Time value={entry.updated_at} mode="datetime" /></dd>
        </dl>
      </div>
    </section>
  </div>

  <div class="stack-lg">
    <section class="card" aria-labelledby="summary-metadata">
      <div class="card__head">
        <h2 id="summary-metadata">Metadata</h2>
        {#if selectedVersion}
          <span class="muted small">Version <span class="mono">{selectedVersion}</span></span>
        {/if}
      </div>
      <div class="card__body">
        {#if !detail}
          <p class="muted">Loading metadata&hellip;</p>
        {:else if metadataEntries.length === 0}
          <p class="muted">
            This version declares no fields beyond the standard ones (name, description, version,
            tags and category).
          </p>
        {:else}
          <dl class="dl dl-compact">
            {#each metadataEntries as [key, value] (key)}
              {@const shown = renderValue(value)}
              <dt class="mono">{key}</dt>
              <dd>
                {#if shown.block}
                  <pre class="metadata-block">{shown.text}</pre>
                {:else}
                  {shown.text}
                {/if}
              </dd>
            {/each}
          </dl>
        {/if}
      </div>
    </section>

    {#if canManage}
      <section class="card" aria-labelledby="summary-manage">
        <div class="card__head">
          <h2 id="summary-manage">Visibility</h2>
        </div>
        <div class="card__body stack">
          {#if entry.status === 'unpublished'}
            <p class="muted">
              This entry is hidden. Restoring it makes it visible again, as Published when it has a
              published version and as Draft otherwise.
            </p>
            <div>
              <button
                type="button"
                id="visibility-action"
                class="btn btn-primary"
                onclick={() => (confirming = 'restore')}
              >
                Restore entry
              </button>
            </div>
          {:else}
            <p class="muted">
              Hiding removes the entry from search and from the public registry for everyone except
              you and the admins. No version is deleted.
            </p>
            <div>
              <button
                type="button"
                id="visibility-action"
                class="btn btn-outline-danger"
                onclick={() => (confirming = 'hide')}
              >
                Hide entry
              </button>
            </div>
          {/if}
        </div>
      </section>
    {:else}
      <Alert tone="info" live={false}>
        Only the owner of this entry and admins can change it. You can read and download it.
      </Alert>
    {/if}
  </div>
</div>

<ConfirmDialog
  open={confirming === 'hide'}
  title="Hide {entry.display_name}?"
  confirmLabel="Hide entry"
  tone="danger"
  {busy}
  error={failure}
  onconfirm={hide}
  oncancel={closeDialog}
>
  <p>
    The entry disappears from search and from the public registry for everyone except you and the
    admins. No version is deleted, and you can restore it at any time.
  </p>
</ConfirmDialog>

<ConfirmDialog
  open={confirming === 'restore'}
  title="Restore {entry.display_name}?"
  confirmLabel="Restore entry"
  {busy}
  error={failure}
  onconfirm={restore}
  oncancel={closeDialog}
>
  <p>
    The entry becomes visible again. Its status will be Published if it has a published version and
    Draft otherwise.
  </p>
</ConfirmDialog>
