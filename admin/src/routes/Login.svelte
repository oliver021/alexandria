<script>
  import { api } from '../lib/api.js'
  import { session } from '../lib/auth.svelte.js'
  import Alert from '../lib/components/Alert.svelte'
  import Logo from '../lib/components/Logo.svelte'
  import { focusAndReveal } from '../lib/focus.js'
  import { describeError } from '../lib/reasons.js'
  import { router } from '../lib/router.svelte.js'
  import { safeNext } from '../lib/routes.js'

  let username = $state('')
  let password = $state('')
  let busy = $state(false)
  let error = $state('')

  /**
   * @param {SubmitEvent} event
   */
  async function submit(event) {
    event.preventDefault()
    if (busy) return
    busy = true
    error = ''
    try {
      const { token, user } = await api.login(username, password)
      session.start(token, user)
      password = ''
      router.navigate(safeNext(router.route.query.get('next')), { replace: true })
    } catch (cause) {
      password = ''
      error =
        /** @type {any} */ (cause)?.status === 401
          ? 'The username or password is not correct.'
          : describeError(cause).message
    } finally {
      busy = false
    }
    if (error) await focusAndReveal('login-error')
  }
</script>

<div class="login">
  <div class="login__brand">
    <Logo size={40} />
    <h1>Alexandria</h1>
    <p class="muted">Sign in to manage skills and grounding packages.</p>
  </div>

  <form class="card login__card" onsubmit={submit}>
    <div class="card__body stack">
      {#if session.notice}
        <Alert tone="warning">{session.notice}</Alert>
      {/if}
      {#if error}
        <Alert tone="danger" id="login-error" focusable>{error}</Alert>
      {/if}
      <div class="field">
        <label class="field-label" for="login-username">Username</label>
        <input
          id="login-username"
          class="input"
          type="text"
          name="username"
          autocomplete="username"
          autocapitalize="none"
          spellcheck="false"
          required
          bind:value={username}
        />
      </div>
      <div class="field">
        <label class="field-label" for="login-password">Password</label>
        <input
          id="login-password"
          class="input"
          type="password"
          name="password"
          autocomplete="current-password"
          required
          bind:value={password}
        />
      </div>
      <button type="submit" class="btn btn-primary" disabled={busy}>
        {#if busy}<span class="spinner" aria-hidden="true"></span>{/if}
        {busy ? 'Signing in…' : 'Sign in'}
      </button>
    </div>
  </form>

  <p class="login__foot muted small">
    Looking for the catalog? <a href="/registry">Open the public registry</a>.
  </p>
</div>
