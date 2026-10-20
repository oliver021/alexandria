<script>
  import { onMount } from 'svelte'
  import { api, isMissingEndpoint } from '../lib/api.js'
  import { session } from '../lib/auth.svelte.js'
  import Alert from '../lib/components/Alert.svelte'
  import Badge from '../lib/components/Badge.svelte'
  import ConfirmDialog from '../lib/components/ConfirmDialog.svelte'
  import EmptyState from '../lib/components/EmptyState.svelte'
  import ErrorState from '../lib/components/ErrorState.svelte'
  import Icon from '../lib/components/Icon.svelte'
  import LoadingState from '../lib/components/LoadingState.svelte'
  import PageHeader from '../lib/components/PageHeader.svelte'
  import Time from '../lib/components/Time.svelte'
  import { focusAndReveal, focusById, focusFirstInvalid } from '../lib/focus.js'
  import { roleLabel } from '../lib/format.js'
  import { describeError } from '../lib/reasons.js'
  import { useResource } from '../lib/resource.svelte.js'
  import { toasts } from '../lib/toast.svelte.js'

  const MIN_PASSWORD = 8
  const MAX_PASSWORD = 1024
  const UNSUPPORTED_MESSAGE =
    'This registry does not offer user deactivation through its API yet, so the account was not changed.'

  const users = useResource((signal) => api.listUsers({ signal }))
  const deactivation = useResource(() => api.checkUserDeactivation())

  let username = $state('')
  let email = $state('')
  let password = $state('')
  let role = $state('publisher')
  let showPassword = $state(false)
  let creating = $state(false)
  let submitted = $state(false)
  let createError = $state('')
  let form = $state(/** @type {HTMLFormElement | undefined} */ (undefined))
  /** @type {import('../lib/types.js').User | null} */
  let deactivating = $state(null)
  let deactivateBusy = $state(false)
  let deactivateError = $state('')

  const list = $derived(users.data ?? [])
  const canDeactivate = $derived(deactivation.data === 'available')
  const problems = $derived({
    username: username.trim() === '' ? 'Enter a username.' : '',
    email: /^[^\s@]+@[^\s@]+$/.test(email.trim()) ? '' : 'Enter a valid email address.',
    password:
      password.length < MIN_PASSWORD || password.length > MAX_PASSWORD
        ? `Use between ${MIN_PASSWORD} and ${MAX_PASSWORD} characters.`
        : '',
  })
  const hasProblems = $derived(Object.values(problems).some(Boolean))

  onMount(() => {
    if (!session.isAdmin) return
    users.load()
    deactivation.load()
  })

  /**
   * @param {SubmitEvent} event
   */
  async function create(event) {
    event.preventDefault()
    submitted = true
    createError = ''
    if (creating) return
    if (hasProblems) {
      // The errors sit next to their fields and the button is at the bottom of the form.
      await focusFirstInvalid(form)
      return
    }
    creating = true
    let failed = false
    try {
      const created = await api.createUser({
        username: username.trim(),
        email: email.trim(),
        password,
        role,
      })
      toasts.success(`User ${created.username} was created.`)
      username = ''
      email = ''
      password = ''
      role = 'publisher'
      showPassword = false
      submitted = false
      await users.reload()
    } catch (error) {
      createError = describeError(error).message
      failed = true
    } finally {
      creating = false
    }
    if (failed) await focusAndReveal('user-create-error')
  }

  /**
   * @param {import('../lib/types.js').User} user
   */
  function askDeactivate(user) {
    if (user.id === session.user?.id) return
    deactivateError = ''
    deactivating = user
  }

  function cancelDeactivate() {
    if (!deactivateBusy) deactivating = null
  }

  async function deactivate() {
    const target = deactivating
    if (!target || target.id === session.user?.id) return
    deactivateBusy = true
    deactivateError = ''
    try {
      await api.deactivateUser(target.id)
      deactivating = null
      toasts.success(`${target.username} was deactivated.`)
      await users.reload()
      await focusById('users-list')
    } catch (error) {
      deactivateError = isMissingEndpoint(error)
        ? UNSUPPORTED_MESSAGE
        : describeError(error).message
    } finally {
      deactivateBusy = false
    }
  }
</script>

<PageHeader
  title="Users"
  lead="Create accounts and see who can sign in. Only admins can see this page."
/>

{#if !session.isAdmin}
  <div class="card">
    <EmptyState
      icon="shield"
      title="Admins only"
      text="Your account does not have the admin role, so you cannot manage users."
    />
  </div>
{:else}
  <div class="stack-lg">
    <div class="two-col">
      <section class="card" aria-labelledby="users-create">
        <div class="card__head">
          <h2 id="users-create">Create a user</h2>
        </div>
        <form class="card__body stack" onsubmit={create} aria-labelledby="users-create" bind:this={form}>
          {#if createError}
            <Alert tone="danger" title="The user was not created" id="user-create-error" focusable>
              {createError}
            </Alert>
          {/if}

          <div class="field">
            <label class="field-label" for="user-username">Username</label>
            <input
              id="user-username"
              class="input"
              type="text"
              name="username"
              maxlength="255"
              autocomplete="off"
              autocapitalize="none"
              spellcheck="false"
              aria-invalid={submitted && problems.username !== ''}
              aria-describedby={submitted && problems.username ? 'user-username-error' : undefined}
              bind:value={username}
            />
            {#if submitted && problems.username}
              <p class="field-error" id="user-username-error" role="alert">{problems.username}</p>
            {/if}
          </div>

          <div class="field">
            <label class="field-label" for="user-email">Email</label>
            <input
              id="user-email"
              class="input"
              type="email"
              name="email"
              maxlength="255"
              autocomplete="off"
              aria-invalid={submitted && problems.email !== ''}
              aria-describedby={submitted && problems.email ? 'user-email-error' : undefined}
              bind:value={email}
            />
            {#if submitted && problems.email}
              <p class="field-error" id="user-email-error" role="alert">{problems.email}</p>
            {/if}
          </div>

          <div class="field">
            <label class="field-label" for="user-password">Initial password</label>
            <div class="input-with-action">
              <input
                id="user-password"
                class="input"
                type={showPassword ? 'text' : 'password'}
                name="password"
                autocomplete="new-password"
                spellcheck="false"
                aria-invalid={submitted && problems.password !== ''}
                aria-describedby="user-password-hint{submitted && problems.password
                  ? ' user-password-error'
                  : ''}"
                bind:value={password}
              />
              <button
                type="button"
                class="btn"
                aria-pressed={showPassword}
                onclick={() => (showPassword = !showPassword)}
              >
                <Icon name={showPassword ? 'eye-off' : 'eye'} />
                {showPassword ? 'Hide' : 'Show'}
                <span class="visually-hidden">password</span>
              </button>
            </div>
            <p class="field-hint" id="user-password-hint">
              Between {MIN_PASSWORD} and {MAX_PASSWORD} characters. Share it through a secure
              channel.
            </p>
            {#if submitted && problems.password}
              <p class="field-error" id="user-password-error" role="alert">{problems.password}</p>
            {/if}
          </div>

          <div class="field">
            <label class="field-label" for="user-role">Role</label>
            <select id="user-role" class="select" bind:value={role} aria-describedby="user-role-hint">
              <option value="publisher">Publisher</option>
              <option value="admin">Admin</option>
            </select>
            <p class="field-hint" id="user-role-hint">
              Publishers upload and manage their own entries. Admins manage everything, including
              users.
            </p>
          </div>

          <div>
            <button type="submit" class="btn btn-primary" disabled={creating}>
              {#if creating}<span class="spinner" aria-hidden="true"></span>{/if}
              Create user
            </button>
          </div>
        </form>
      </section>

      <aside class="card" aria-labelledby="users-help">
        <div class="card__head">
          <h2 id="users-help">About accounts</h2>
        </div>
        <div class="card__body">
          <ul class="bullets">
            <li>There is no self-registration: every account is created here.</li>
            {#if canDeactivate}
              <li>
                A deactivated account cannot sign in and all of its API tokens stop working. Its
                entries stay in the registry.
              </li>
              <li>You cannot deactivate your own account, so the registry always keeps an admin.</li>
            {:else if deactivation.data === 'missing'}
              <li>
                The registry's API has no operation to deactivate accounts, so this console cannot
                offer it.
              </li>
            {:else if deactivation.data === 'unknown'}
              <li>
                This console could not read the description of the registry's API, so it does not
                offer to deactivate accounts. Reload the page to try again.
              </li>
            {/if}
          </ul>
        </div>
      </aside>
    </div>

    <section aria-labelledby="users-list">
      <div class="section-head">
        <h2 id="users-list" tabindex="-1">All users</h2>
        {#if users.loaded}
          <button type="button" class="btn btn-sm btn-quiet" onclick={() => users.reload()}>
            <Icon name="refresh" size={14} />
            Refresh
          </button>
        {/if}
      </div>

      {#if users.error && !users.loaded}
        <div class="card"><ErrorState error={users.error} onretry={users.reload} /></div>
      {:else if !users.loaded}
        <div class="card"><LoadingState label="Loading users&hellip;" /></div>
      {:else if list.length === 0}
        <div class="card"><EmptyState icon="users" title="There are no users" /></div>
      {:else}
        {#if users.error}
          <p class="field-error" role="alert">
            The list could not be refreshed.
            <button type="button" class="btn btn-sm" onclick={() => users.reload()}>Try again</button>
          </p>
        {/if}
        <div class="table-wrap">
          <table class="table">
            <caption class="visually-hidden">User accounts</caption>
            <thead>
              <tr>
                <th scope="col">Username</th>
                <th scope="col">Email</th>
                <th scope="col">Role</th>
                <th scope="col">Status</th>
                <th scope="col" class="hide-narrow">Created</th>
                {#if canDeactivate}
                  <th scope="col"><span class="visually-hidden">Actions</span></th>
                {/if}
              </tr>
            </thead>
            <tbody>
              {#each list as user (user.id)}
                <tr class={[!user.is_active && 'row-muted']}>
                  <th scope="row" class="cell-text">
                    {user.username}
                    {#if user.id === session.user?.id}<Badge tone="info">You</Badge>{/if}
                  </th>
                  <td class="cell-text">{user.email}</td>
                  <td>
                    <Badge tone={user.role === 'admin' ? 'skill' : 'neutral'}>
                      {roleLabel(user.role)}
                    </Badge>
                  </td>
                  <td>
                    {#if user.is_active}
                      <Badge tone="ok">Active</Badge>
                    {:else}
                      <Badge tone="neutral">Inactive</Badge>
                    {/if}
                  </td>
                  <td class="hide-narrow"><Time value={user.created_at} /></td>
                  {#if canDeactivate}
                    <td class="actions">
                      {#if user.id === session.user?.id}
                        <span class="muted small">This is your account</span>
                      {:else if user.is_active}
                        <button
                          type="button"
                          class="btn btn-sm btn-outline-danger"
                          onclick={() => askDeactivate(user)}
                        >
                          Deactivate <span class="visually-hidden">{user.username}</span>
                        </button>
                      {/if}
                    </td>
                  {/if}
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {/if}
    </section>
  </div>
{/if}

{#if canDeactivate}
  <ConfirmDialog
    open={deactivating !== null}
    title="Deactivate {deactivating?.username ?? 'user'}?"
    confirmLabel="Deactivate user"
    tone="danger"
    busy={deactivateBusy}
    error={deactivateError}
    onconfirm={deactivate}
    oncancel={cancelDeactivate}
  >
    <p>
      <strong>{deactivating?.username}</strong> will no longer be able to sign in, and every API
      token they own stops working immediately. Their entries stay in the registry. Accounts cannot
      be reactivated from this console.
    </p>
  </ConfirmDialog>
{/if}
