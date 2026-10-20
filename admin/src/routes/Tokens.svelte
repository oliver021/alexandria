<script>
  import { onMount } from 'svelte'
  import { api } from '../lib/api.js'
  import Alert from '../lib/components/Alert.svelte'
  import Badge from '../lib/components/Badge.svelte'
  import ConfirmDialog from '../lib/components/ConfirmDialog.svelte'
  import CopyButton from '../lib/components/CopyButton.svelte'
  import EmptyState from '../lib/components/EmptyState.svelte'
  import ErrorState from '../lib/components/ErrorState.svelte'
  import Icon from '../lib/components/Icon.svelte'
  import LoadingState from '../lib/components/LoadingState.svelte'
  import PageHeader from '../lib/components/PageHeader.svelte'
  import Time from '../lib/components/Time.svelte'
  import { focusAndReveal, focusById, focusFirstInvalid } from '../lib/focus.js'
  import { tokenState } from '../lib/format.js'
  import { describeError } from '../lib/reasons.js'
  import { useResource } from '../lib/resource.svelte.js'
  import { toasts } from '../lib/toast.svelte.js'

  const EXPIRY_OPTIONS = [
    { value: 'never', label: 'No expiry' },
    { value: '7', label: '7 days' },
    { value: '30', label: '30 days' },
    { value: '90', label: '90 days' },
    { value: '180', label: '180 days' },
    { value: '365', label: '1 year' },
    { value: 'custom', label: 'Custom number of days' },
  ]
  const MAX_DAYS = 3650

  const tokens = useResource((signal) => api.listTokens({ signal }))

  let name = $state('')
  let scope = $state('read')
  let expiry = $state('90')
  /** @type {string | number | null} */
  let customDays = $state('')
  let creating = $state(false)
  let submitted = $state(false)
  let createError = $state('')
  let form = $state(/** @type {HTMLFormElement | undefined} */ (undefined))
  /** @type {import('../lib/types.js').CreatedToken | null} */
  let fresh = $state(null)
  /** @type {import('../lib/types.js').ApiToken | null} */
  let revoking = $state(null)
  let revokeBusy = $state(false)
  let revokeError = $state('')

  const nameProblem = $derived(name.trim() === '' ? 'Give the token a name so you can recognise it later.' : '')
  const daysProblem = $derived.by(() => {
    if (expiry !== 'custom') return ''
    const raw = String(customDays ?? '').trim()
    const days = Number(raw)
    if (raw === '' || !Number.isInteger(days) || days < 1 || days > MAX_DAYS) {
      return `Enter a whole number of days between 1 and ${MAX_DAYS}.`
    }
    return ''
  })
  const list = $derived(tokens.data ?? [])

  onMount(() => {
    tokens.load()
  })

  /**
   * @returns {number | null}
   */
  function chosenDays() {
    if (expiry === 'never') return null
    return Number(expiry === 'custom' ? customDays : expiry)
  }

  /**
   * @param {SubmitEvent} event
   */
  async function create(event) {
    event.preventDefault()
    submitted = true
    createError = ''
    if (creating || fresh) return
    if (nameProblem || daysProblem) {
      // The errors sit next to their fields and the button is at the bottom of the form.
      await focusFirstInvalid(form)
      return
    }
    creating = true
    let failed = false
    try {
      const days = chosenDays()
      /** @type {{ name: string, scope: string, expires_in_days?: number }} */
      const payload = { name: name.trim(), scope }
      if (days !== null) payload.expires_in_days = days
      fresh = await api.createToken(payload)
      name = ''
      submitted = false
      toasts.success('API token created. Copy it now: it is shown only once.')
      await tokens.reload()
      await focusById('new-token-value')
    } catch (error) {
      createError = describeError(error).message
      failed = true
    } finally {
      creating = false
    }
    if (failed) await focusAndReveal('token-create-error')
  }

  function dismissFresh() {
    fresh = null
  }

  /**
   * @param {import('../lib/types.js').ApiToken} token
   */
  function askRevoke(token) {
    revokeError = ''
    revoking = token
  }

  function cancelRevoke() {
    if (!revokeBusy) revoking = null
  }

  async function revoke() {
    if (!revoking) return
    const target = revoking
    revokeBusy = true
    revokeError = ''
    try {
      await api.revokeToken(target.id)
      revoking = null
      toasts.success(`Token "${target.name}" was revoked.`)
      await tokens.reload()
      await focusById('tokens-list')
    } catch (error) {
      revokeError = describeError(error).message
    } finally {
      revokeBusy = false
    }
  }
</script>

<PageHeader
  title="API tokens"
  lead="Tokens let agents, scripts and CI use the registry without your password. Send one as an Authorization: Bearer header."
/>

<div class="stack-lg">
  {#if fresh}
    <section class="card reveal" aria-labelledby="reveal-title">
      <div class="card__body stack">
        <Alert tone="warning" title="Copy your new token now">
          <p>
            This is the only time it is shown. The registry stores just a hash of it, so it cannot
            be displayed again. Keep it somewhere safe, such as a password manager or a secrets
            store.
          </p>
        </Alert>
        <h2 id="reveal-title" class="visually-hidden">New token</h2>
        <div class="field">
          <label class="field-label" for="new-token-value">
            Token &ldquo;{fresh.name}&rdquo;
            <Badge tone={fresh.scope === 'write' ? 'warn' : 'info'}>{fresh.scope}</Badge>
          </label>
          <div class="input-with-action">
            <input
              id="new-token-value"
              class="input input-mono"
              type="text"
              readonly
              spellcheck="false"
              autocomplete="off"
              value={fresh.token}
              onfocus={(event) => event.currentTarget.select()}
            />
            <CopyButton text={fresh.token} label="Copy token" small={false} />
          </div>
        </div>
        <div>
          <button type="button" class="btn" onclick={dismissFresh}>I have stored it safely</button>
        </div>
      </div>
    </section>
  {/if}

  <div class="two-col">
    <section class="card" aria-labelledby="tokens-create">
      <div class="card__head">
        <h2 id="tokens-create">Create a token</h2>
      </div>
      <form class="card__body stack" onsubmit={create} aria-labelledby="tokens-create" bind:this={form}>
        {#if createError}
          <Alert tone="danger" title="The token was not created" id="token-create-error" focusable>
            {createError}
          </Alert>
        {/if}

        <div class="field">
          <label class="field-label" for="token-name">Name</label>
          <input
            id="token-name"
            class="input"
            type="text"
            name="name"
            maxlength="100"
            autocomplete="off"
            placeholder="CI pipeline"
            aria-describedby="token-name-hint{submitted && nameProblem ? ' token-name-error' : ''}"
            aria-invalid={submitted && nameProblem !== ''}
            bind:value={name}
          />
          <p class="field-hint" id="token-name-hint">
            A label to recognise the token in this list, up to 100 characters.
          </p>
          {#if submitted && nameProblem}
            <p class="field-error" id="token-name-error" role="alert">{nameProblem}</p>
          {/if}
        </div>

        <div class="field">
          <label class="field-label" for="token-scope">Scope</label>
          <select id="token-scope" class="select" bind:value={scope} aria-describedby="token-scope-hint">
            <option value="read">Read: search and download</option>
            <option value="write">Write: also upload, publish and manage tokens</option>
          </select>
          <p class="field-hint" id="token-scope-hint">
            Prefer read for agents that only consume entries. A token never has more permissions
            than your account.
          </p>
        </div>

        <div class="field">
          <label class="field-label" for="token-expiry">Expires</label>
          <select id="token-expiry" class="select" bind:value={expiry}>
            {#each EXPIRY_OPTIONS as option (option.value)}
              <option value={option.value}>{option.label}</option>
            {/each}
          </select>
        </div>

        {#if expiry === 'custom'}
          <div class="field">
            <label class="field-label" for="token-days">Days until it expires</label>
            <input
              id="token-days"
              class="input"
              type="number"
              min="1"
              max={MAX_DAYS}
              step="1"
              inputmode="numeric"
              aria-describedby={submitted && daysProblem ? 'token-days-error' : undefined}
              aria-invalid={submitted && daysProblem !== ''}
              bind:value={customDays}
            />
            {#if submitted && daysProblem}
              <p class="field-error" id="token-days-error" role="alert">{daysProblem}</p>
            {/if}
          </div>
        {/if}

        <div class="cluster">
          <button type="submit" class="btn btn-primary" disabled={creating || fresh !== null}>
            {#if creating}<span class="spinner" aria-hidden="true"></span>{/if}
            Create token
          </button>
          {#if fresh}
            <span class="muted small">Dismiss the new token above to create another.</span>
          {/if}
        </div>
      </form>
    </section>

    <aside class="card" aria-labelledby="tokens-help">
      <div class="card__head">
        <h2 id="tokens-help">How tokens work</h2>
      </div>
      <div class="card__body">
        <ul class="bullets">
          <li>The secret starts with <code>alx_</code> and is shown once, right after creation.</li>
          <li>
            Use it as <code>Authorization: Bearer alx_&hellip;</code> in REST calls, MCP clients and
            the <code>alx</code> command line.
          </li>
          <li>You can have a limited number of active tokens. Revoke the ones you no longer use.</li>
          <li>Deactivating the owner or revoking a token cuts off access immediately.</li>
        </ul>
      </div>
    </aside>
  </div>

  <section aria-labelledby="tokens-list">
    <div class="section-head">
      <h2 id="tokens-list" tabindex="-1">Your tokens</h2>
      {#if tokens.loaded}
        <button type="button" class="btn btn-sm btn-quiet" onclick={() => tokens.reload()}>
          <Icon name="refresh" size={14} />
          Refresh
        </button>
      {/if}
    </div>

    {#if tokens.error && !tokens.loaded}
      <div class="card"><ErrorState error={tokens.error} onretry={tokens.reload} /></div>
    {:else if !tokens.loaded}
      <div class="card"><LoadingState label="Loading tokens&hellip;" /></div>
    {:else if list.length === 0}
      <div class="card">
        <EmptyState
          icon="key"
          title="You have no tokens yet"
          text="Create one above to give an agent or a script access without sharing your password."
        />
      </div>
    {:else}
      {#if tokens.error}
        <p class="field-error" role="alert">
          The list could not be refreshed.
          <button type="button" class="btn btn-sm" onclick={() => tokens.reload()}>Try again</button>
        </p>
      {/if}
      <div class="table-wrap">
        <table class="table">
          <caption class="visually-hidden">Your API tokens</caption>
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Token</th>
              <th scope="col">Scope</th>
              <th scope="col" class="hide-narrow">Created</th>
              <th scope="col">Expires</th>
              <th scope="col" class="hide-narrow">Last used</th>
              <th scope="col">Status</th>
              <th scope="col"><span class="visually-hidden">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {#each list as token (token.id)}
              {@const phase = tokenState(token)}
              <tr class={[phase !== 'active' && 'row-muted']}>
                <th scope="row" class="cell-text">{token.name}</th>
                <td class="cell-code"><code>{token.prefix}&hellip;</code></td>
                <td>
                  <Badge tone={token.scope === 'write' ? 'warn' : 'info'}>{token.scope}</Badge>
                </td>
                <td class="hide-narrow"><Time value={token.created_at} /></td>
                <td>
                  {#if token.expires_at}
                    <Time value={token.expires_at} />
                  {:else}
                    <span class="muted">Never</span>
                  {/if}
                </td>
                <td class="hide-narrow">
                  {#if token.last_used_at}
                    <Time value={token.last_used_at} mode="relative" />
                  {:else}
                    <span class="muted">Never</span>
                  {/if}
                </td>
                <td>
                  {#if phase === 'revoked'}
                    <Badge tone="neutral">Revoked</Badge>
                  {:else if phase === 'expired'}
                    <Badge tone="warn">Expired</Badge>
                  {:else}
                    <Badge tone="ok">Active</Badge>
                  {/if}
                </td>
                <td class="actions">
                  {#if phase === 'active'}
                    <button
                      type="button"
                      class="btn btn-sm btn-outline-danger"
                      onclick={() => askRevoke(token)}
                    >
                      Revoke <span class="visually-hidden">token {token.name}</span>
                    </button>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </section>
</div>

<ConfirmDialog
  open={revoking !== null}
  title="Revoke {revoking?.name ?? 'token'}?"
  confirmLabel="Revoke token"
  tone="danger"
  busy={revokeBusy}
  error={revokeError}
  onconfirm={revoke}
  oncancel={cancelRevoke}
>
  <p>
    Anything that still uses this token, such as an agent, a script or a pipeline, loses access
    immediately. A revoked token cannot be restored: create a new one instead.
  </p>
</ConfirmDialog>
