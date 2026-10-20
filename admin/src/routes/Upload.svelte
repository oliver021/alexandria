<script>
  import { onDestroy, onMount } from 'svelte'
  import { api, isAbortError, isUnauthorized } from '../lib/api.js'
  import Alert from '../lib/components/Alert.svelte'
  import Icon from '../lib/components/Icon.svelte'
  import KindBadge from '../lib/components/KindBadge.svelte'
  import PageHeader from '../lib/components/PageHeader.svelte'
  import StatusBadge from '../lib/components/StatusBadge.svelte'
  import { focusAndReveal, focusById } from '../lib/focus.js'
  import { formatBytes, pluralize } from '../lib/format.js'
  import { describeUploadError } from '../lib/reasons.js'
  import { useResource } from '../lib/resource.svelte.js'
  import { entryHref } from '../lib/routes.js'
  import { toasts } from '../lib/toast.svelte.js'
  import { validatePackageFile, validateVersion } from '../lib/upload.js'

  const info = useResource((signal) => api.info({ signal }))

  let file = $state(/** @type {File | null} */ (null))
  let version = $state('')
  let publish = $state(false)
  let dragging = $state(false)
  let extraFiles = $state(false)
  /** @typedef {'idle' | 'uploading' | 'processing' | 'done' | 'failed'} Phase */
  let phase = $state(/** @type {Phase} */ ('idle'))
  let loaded = $state(0)
  let transferTotal = $state(0)
  let result = $state(/** @type {import('../lib/types.js').UploadResult | null} */ (null))
  let failure = $state(/** @type {import('../lib/reasons.js').ErrorDescription | null} */ (null))
  let versionTouched = $state(false)
  /** @type {AbortController | null} */
  let controller = null
  /** @type {HTMLElement | undefined} */
  let zone = $state()
  /** @type {HTMLInputElement | undefined} */
  let input = $state()

  const limits = $derived(info.data?.limits ?? null)
  const fileProblems = $derived(file ? validatePackageFile(file, limits) : [])
  const versionProblem = $derived(validateVersion(version))
  const busy = $derived(phase === 'uploading' || phase === 'processing')
  const canSubmit = $derived(Boolean(file) && fileProblems.length === 0 && !versionProblem && !busy)
  const percent = $derived(
    transferTotal > 0 ? Math.min(100, Math.round((loaded / transferTotal) * 100)) : 0,
  )
  const announcement = $derived(
    phase === 'uploading'
      ? 'Uploading the package.'
      : phase === 'processing'
        ? 'Upload finished. The registry is checking the package.'
        : phase === 'done'
          ? 'The package was uploaded.'
          : phase === 'failed'
            ? 'The upload failed.'
            : '',
  )

  onMount(() => {
    info.load()
  })

  onDestroy(() => controller?.abort())

  /**
   * @param {FileList | File[] | null | undefined} list
   */
  function choose(list) {
    const picked = list ? Array.from(list) : []
    if (picked.length === 0) return
    extraFiles = picked.length > 1
    file = picked[0]
    phase = 'idle'
    result = null
    failure = null
    loaded = 0
    transferTotal = 0
  }

  function clearFile() {
    file = null
    extraFiles = false
    phase = 'idle'
    failure = null
    if (input) input.value = ''
  }

  function reset() {
    clearFile()
    result = null
    version = ''
    versionTouched = false
    publish = false
  }

  function cancelUpload() {
    controller?.abort()
  }

  /**
   * @param {SubmitEvent} event
   */
  async function submit(event) {
    event.preventDefault()
    versionTouched = true
    if (!file || !canSubmit) return
    const chosen = file
    phase = 'uploading'
    loaded = 0
    transferTotal = chosen.size
    failure = null
    result = null
    const current = new AbortController()
    controller = current
    try {
      const uploaded = await api.uploadPackage(chosen, {
        version: version.trim() || undefined,
        publish,
        signal: current.signal,
        onProgress: (progress) => {
          loaded = progress.loaded
          transferTotal = progress.total
          if (progress.done) phase = 'processing'
        },
      })
      result = uploaded
      phase = 'done'
      toasts.success(`Uploaded ${uploaded.entry.name} ${uploaded.version.version}.`)
      await focusById('upload-result')
    } catch (error) {
      if (isAbortError(error)) {
        phase = 'idle'
        toasts.info('The upload was cancelled.')
      } else if (isUnauthorized(error)) {
        phase = 'idle'
      } else {
        failure = describeUploadError(error, limits)
        phase = 'failed'
      }
    } finally {
      if (controller === current) controller = null
    }
    // The explanation is drawn above the form and the button is below it: bring it into view.
    if (phase === 'failed') await focusAndReveal('upload-failure')
  }

  $effect(() => {
    const element = zone
    if (!element) return undefined
    /** @param {DragEvent} event */
    const over = (event) => {
      event.preventDefault()
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy'
      dragging = true
    }
    /** @param {DragEvent} event */
    const leave = (event) => {
      const next = event.relatedTarget
      if (!(next instanceof Node) || !element.contains(next)) dragging = false
    }
    /** @param {DragEvent} event */
    const drop = (event) => {
      event.preventDefault()
      dragging = false
      if (!busy) choose(event.dataTransfer?.files)
    }
    element.addEventListener('dragenter', over)
    element.addEventListener('dragover', over)
    element.addEventListener('dragleave', leave)
    element.addEventListener('drop', drop)
    return () => {
      element.removeEventListener('dragenter', over)
      element.removeEventListener('dragover', over)
      element.removeEventListener('dragleave', leave)
      element.removeEventListener('drop', drop)
    }
  })
</script>

<PageHeader
  title="Upload package"
  lead="Upload a .zip with a SKILL.md or INDEX.md at its root. The registry validates it before anything is stored, and every upload is a new, immutable version."
/>

<span class="visually-hidden" role="status">{announcement}</span>

<div class="two-col">
  <div class="stack-lg">
    {#if phase === 'done' && result}
      <section class="card" id="upload-result" tabindex="-1" aria-labelledby="upload-done-title">
        <div class="card__body stack">
          <Alert tone="success" title="Package uploaded">
            <p>
              <strong class="mono">{result.entry.name}</strong> version
              <strong class="mono">{result.version.version}</strong> was stored.
            </p>
          </Alert>
          <h2 id="upload-done-title" class="visually-hidden">Upload result</h2>
          <dl class="dl dl-compact">
            <dt>Entry</dt>
            <dd>
              <a href={entryHref(result.entry.name)}>{result.entry.display_name}</a>
              <KindBadge kind={result.entry.kind} />
            </dd>
            <dt>Version</dt>
            <dd>
              <span class="mono">{result.version.version}</span>
              <StatusBadge status={result.version.status} />
            </dd>
            <dt>Size</dt>
            <dd>
              {formatBytes(result.version.size_bytes)} zipped,
              {pluralize(result.version.file_count, 'file')},
              {formatBytes(result.version.uncompressed_bytes)} unpacked
            </dd>
            <dt>SHA-256</dt>
            <dd class="mono wrap-anywhere">{result.version.sha256}</dd>
          </dl>
          {#if result.version.status === 'draft'}
            <p class="muted">
              The version is a draft: only you and the admins can see it. Publish it from the
              Versions tab when it is ready.
            </p>
          {/if}
          <div class="cluster">
            <a
              class="btn btn-primary"
              href={entryHref(result.entry.name, { tab: 'versions' })}
            >
              Open the entry
            </a>
            <button type="button" class="btn" onclick={reset}>Upload another package</button>
          </div>
        </div>
      </section>
    {:else}
      <form class="card" onsubmit={submit} aria-label="Upload a package">
        <div class="card__body stack-lg">
          {#if phase === 'failed' && failure}
            <Alert tone="danger" title={failure.title} id="upload-failure" focusable>
              <p>{failure.message}</p>
              {#if failure.hint}<p>{failure.hint}</p>{/if}
              {#if failure.serverMessage && failure.serverMessage !== failure.message}
                <p class="small">Server message: <span class="mono">{failure.serverMessage}</span></p>
              {/if}
              {#if failure.reason}
                <p class="small">Reason code: <code>{failure.reason}</code></p>
              {/if}
            </Alert>
          {/if}

          <div class="field">
            <span class="field-label" id="upload-file-label">Package file</span>
            <div
              class={['dropzone', dragging && 'dropzone-active', file && 'dropzone-filled']}
              bind:this={zone}
            >
              <Icon name="upload" size={28} />
              {#if file}
                <p class="dropzone__file">
                  <strong class="wrap-anywhere">{file.name}</strong>
                  <span class="muted">{formatBytes(file.size)}</span>
                </p>
                {#if !busy}
                  <div class="cluster">
                    <label class="btn btn-sm" for="upload-file">Choose another file</label>
                    <button type="button" class="btn btn-sm btn-quiet" onclick={clearFile}>
                      Remove
                    </button>
                  </div>
                {/if}
              {:else}
                <p><strong>Drag a .zip here</strong> or choose one from your computer.</p>
                <label class="btn" for="upload-file">Choose a .zip file</label>
              {/if}
              <input
                id="upload-file"
                class="visually-hidden"
                type="file"
                accept=".zip,application/zip,application/x-zip-compressed"
                aria-labelledby="upload-file-label"
                aria-describedby="upload-file-hint{fileProblems.length ? ' upload-file-errors' : ''}"
                aria-invalid={fileProblems.length > 0}
                disabled={busy}
                bind:this={input}
                onchange={(event) => choose(event.currentTarget.files)}
              />
            </div>
            <p class="field-hint" id="upload-file-hint">
              {#if limits}
                Up to {formatBytes(limits.max_package_bytes)} zipped, {pluralize(limits.max_files, 'file')}
                and {formatBytes(limits.max_uncompressed_bytes)} unpacked.
              {:else}
                The registry enforces its own size limits.
              {/if}
            </p>
            {#if extraFiles}
              <p class="field-hint">One package at a time: only the first file was used.</p>
            {/if}
            {#if fileProblems.length > 0}
              <div class="stack-tight" id="upload-file-errors" role="alert">
                {#each fileProblems as problem (problem)}
                  <p class="field-error">{problem}</p>
                {/each}
              </div>
            {/if}
          </div>

          <div class="field">
            <label class="field-label" for="upload-version">
              Version <span class="optional">(optional)</span>
            </label>
            <input
              id="upload-version"
              class="input input-mono"
              type="text"
              name="version"
              autocomplete="off"
              autocapitalize="none"
              spellcheck="false"
              placeholder="1.2.0"
              aria-describedby="upload-version-hint{versionTouched && versionProblem ? ' upload-version-error' : ''}"
              aria-invalid={versionTouched && versionProblem !== ''}
              disabled={busy}
              bind:value={version}
              onblur={() => (versionTouched = true)}
            />
            <p class="field-hint" id="upload-version-hint">
              Leave it empty to use the version in the front matter of the entry point. If you fill
              it in and the package declares one too, both must match.
            </p>
            {#if versionTouched && versionProblem}
              <p class="field-error" id="upload-version-error" role="alert">{versionProblem}</p>
            {/if}
          </div>

          <label class="switch">
            <input type="checkbox" role="switch" disabled={busy} bind:checked={publish} />
            <span class="switch__text">
              <span class="field-label">Publish immediately</span>
              <span class="field-hint">
                {publish
                  ? 'Everyone who can read this registry will see the version as soon as it is stored.'
                  : 'The version is saved as a draft that only you and the admins can see.'}
              </span>
            </span>
          </label>

          {#if busy}
            <div class="stack" aria-live="off">
              {#if phase === 'uploading'}
                <progress
                  class="progress"
                  max="100"
                  value={percent}
                  aria-label="Upload progress"
                ></progress>
                <p class="muted small">
                  {percent}% &middot; {formatBytes(Math.min(loaded, file?.size ?? loaded))} of {formatBytes(file?.size)}
                </p>
              {:else}
                <progress class="progress" aria-label="The registry is checking the package"></progress>
                <p class="muted small">All bytes sent. The registry is validating the package&hellip;</p>
              {/if}
            </div>
          {/if}

          <div class="cluster">
            <button type="submit" class="btn btn-primary" disabled={!canSubmit}>
              {#if busy}<span class="spinner" aria-hidden="true"></span>{/if}
              {busy ? 'Uploading…' : 'Upload package'}
            </button>
            {#if phase === 'uploading'}
              <button type="button" class="btn" onclick={cancelUpload}>Cancel upload</button>
            {/if}
          </div>
        </div>
      </form>
    {/if}
  </div>

  <aside class="card" aria-labelledby="upload-help">
    <div class="card__head">
      <h2 id="upload-help">Before you upload</h2>
    </div>
    <div class="card__body stack">
      <ul class="bullets">
        <li>
          The root of the archive needs exactly one entry point: <code>SKILL.md</code> for a skill
          or <code>INDEX.md</code> for a grounding package.
        </li>
        <li>
          The entry point starts with a YAML block that declares a <code>name</code> and a
          <code>description</code>, and usually a <code>version</code>.
        </li>
        <li>
          A version can never be replaced. To fix a mistake, upload the package again with a new
          version number.
        </li>
        <li>
          Symbolic links, encrypted files, unsafe paths and oversized archives are rejected, and
          nothing in the package is ever executed by the registry.
        </li>
      </ul>
      <p class="muted">
        Validate a folder on your machine first with <code>alx validate</code>, or build the zip
        with <code>alx pack</code>.
      </p>
    </div>
  </aside>
</div>
