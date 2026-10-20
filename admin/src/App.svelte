<script>
  import { onMount, tick, untrack } from 'svelte'
  import { api } from './lib/api.js'
  import { session } from './lib/auth.svelte.js'
  import AppHeader from './lib/components/AppHeader.svelte'
  import ErrorState from './lib/components/ErrorState.svelte'
  import Toasts from './lib/components/Toasts.svelte'
  import { router, setPageTitle } from './lib/router.svelte.js'
  import { loginQuery, paths, safeNext } from './lib/routes.js'
  import Dashboard from './routes/Dashboard.svelte'
  import Entry from './routes/Entry.svelte'
  import Library from './routes/Library.svelte'
  import Login from './routes/Login.svelte'
  import NotFound from './routes/NotFound.svelte'
  import Tokens from './routes/Tokens.svelte'
  import Upload from './routes/Upload.svelte'
  import Users from './routes/Users.svelte'

  const pages = {
    dashboard: Dashboard,
    library: Library,
    entry: Entry,
    upload: Upload,
    tokens: Tokens,
    users: Users,
    'not-found': NotFound,
  }

  const route = $derived(router.route)
  const path = $derived(route.path)
  const title = $derived(route.title)
  const Page = $derived(pages[/** @type {keyof typeof pages} */ (route.name)] ?? NotFound)
  const signedIn = $derived(session.isAuthenticated)

  /** @type {HTMLElement | undefined} */
  let main = $state()
  /** @type {string | null} */
  let previousPath = null

  onMount(() => {
    router.start()
    if (session.isAuthenticated) {
      api
        .me()
        .then((user) => session.updateUser(user))
        .catch(() => undefined)
    }
    return () => router.stop()
  })

  $effect(() => {
    if (route.name === 'login') {
      if (signedIn) {
        router.navigate(safeNext(route.query.get('next')), { replace: true })
      }
    } else if (!signedIn) {
      router.navigate(paths.login(), { replace: true, query: loginQuery(route.hash) })
    }
  })

  $effect(() => {
    const currentPath = path
    const currentTitle = title
    untrack(() => {
      setPageTitle(currentTitle)
      if (previousPath !== null && previousPath !== currentPath) {
        window.scrollTo(0, 0)
        tick().then(() => main?.focus({ preventScroll: true }))
      }
      previousPath = currentPath
    })
  })

  /**
   * @param {MouseEvent} event
   */
  function skipToContent(event) {
    event.preventDefault()
    main?.focus()
  }
</script>

<a class="skip-link" href="#main" onclick={skipToContent}>Skip to main content</a>

<div class="app">
  {#if signedIn && route.name !== 'login'}
    <AppHeader />
  {/if}
  <main id="main" class="app-main container" tabindex="-1" bind:this={main}>
    {#if route.name === 'login' || !signedIn}
      <Login />
    {:else}
      {#key route.path}
        <svelte:boundary onerror={(error) => console.error('The page failed to render', error)}>
          <Page />
          {#snippet failed(error, reset)}
            <div class="card">
              <ErrorState {error} title="This page failed to display" onretry={reset} />
            </div>
          {/snippet}
        </svelte:boundary>
      {/key}
    {/if}
  </main>
  <footer class="app-footer">
    <div class="app-footer__inner container">
      <span>Alexandria Admin</span>
      <span>
        <a href="/registry">Public registry</a>
        &middot;
        <a href="/docs">API reference</a>
      </span>
    </div>
  </footer>
  <Toasts />
</div>
