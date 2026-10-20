<script>
  import { onMount } from 'svelte'
  import { IntegrityError, downloadPackageFile } from '../../lib/actions.js'
  import { api } from '../../lib/api.js'
  import Alert from '../../lib/components/Alert.svelte'
  import CopyButton from '../../lib/components/CopyButton.svelte'
  import ErrorState from '../../lib/components/ErrorState.svelte'
  import Icon from '../../lib/components/Icon.svelte'
  import LoadingState from '../../lib/components/LoadingState.svelte'
  import { formatBytes } from '../../lib/format.js'
  import { describeError } from '../../lib/reasons.js'
  import { useResource } from '../../lib/resource.svelte.js'
  import { toasts } from '../../lib/toast.svelte.js'

  /** @type {{ name: string, version: string, file: import('../../lib/types.js').ManifestEntry, inlineLimit?: number | null }} */
  let { name, version, file, inlineLimit = null } = $props()

  const content = useResource((signal) => api.readFile(name, version, file.path, { signal }))
  let downloading = $state(false)

  onMount(() => {
    if (file.text) content.load()
  })

  async function download() {
    downloading = true
    try {
      await downloadPackageFile(name, version, file.path, file.sha256)
    } catch (error) {
      toasts.error(error instanceof IntegrityError ? error.message : describeError(error).message)
    } finally {
      downloading = false
    }
  }
</script>

<section class="card viewer" aria-labelledby="viewer-title">
  <div class="card__head">
    <h2 id="viewer-title" class="wrap-anywhere mono viewer__path">{file.path}</h2>
    <button type="button" class="btn btn-sm" disabled={downloading} onclick={download}>
      {#if downloading}
        <span class="spinner" aria-hidden="true"></span>
      {:else}
        <Icon name="download" size={14} />
      {/if}
      Download file
    </button>
  </div>
  <div class="card__body stack">
    <dl class="dl dl-compact">
      <dt>Size</dt>
      <dd>{formatBytes(file.size)}</dd>
      <dt>Type</dt>
      <dd class="mono">{file.media_type}</dd>
      <dt>SHA-256</dt>
      <dd>
        <div class="cluster">
          <code class="wrap-anywhere">{file.sha256}</code>
          <CopyButton text={file.sha256} subject="SHA-256 of {file.path}" />
        </div>
      </dd>
    </dl>

    {#if !file.text}
      <Alert tone="info" live={false}>
        This is a binary file, so there is no preview. Download it to open it with the right
        application.
      </Alert>
    {:else if content.error}
      <ErrorState error={content.error} onretry={content.reload} />
    {:else if !content.loaded}
      <LoadingState label="Loading file…" compact />
    {:else if content.data}
      {#if content.data.truncated}
        <Alert tone="warning">
          Only the first part of this file is shown{inlineLimit
            ? `, because the registry previews at most ${formatBytes(inlineLimit)}`
            : ''}. Download the file to read all of it.
        </Alert>
      {/if}
      {#if content.data.content === ''}
        <p class="muted">This file is empty.</p>
      {:else}
        <pre class="viewer__pre"><code>{content.data.content}</code></pre>
      {/if}
    {/if}
  </div>
</section>
