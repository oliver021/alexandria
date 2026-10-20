<script>
  import { session } from '../auth.svelte.js'
  import { roleLabel } from '../format.js'
  import { router } from '../router.svelte.js'
  import { hrefFor, paths } from '../routes.js'
  import Badge from './Badge.svelte'
  import Icon from './Icon.svelte'
  import Logo from './Logo.svelte'

  const items = [
    { names: ['dashboard'], label: 'Dashboard', icon: 'dashboard', path: paths.dashboard() },
    { names: ['library', 'entry'], label: 'Library', icon: 'library', path: paths.library() },
    { names: ['upload'], label: 'Upload', icon: 'upload', path: paths.upload() },
    { names: ['tokens'], label: 'API tokens', icon: 'key', path: paths.tokens() },
    { names: ['users'], label: 'Users', icon: 'users', path: paths.users(), admin: true },
  ]

  const visible = $derived(items.filter((item) => !item.admin || session.isAdmin))

  function signOut() {
    session.end()
    router.navigate(paths.login())
  }
</script>

<header class="app-header">
  <div class="app-header__inner container">
    <a class="brand" href={hrefFor(paths.dashboard())}>
      <Logo />
      <span class="brand__name">Alexandria</span>
      <span class="brand__tag">Admin</span>
    </a>
    <nav class="app-nav" aria-label="Main">
      <ul>
        {#each visible as item (item.label)}
          <li>
            <a
              href={hrefFor(item.path)}
              aria-current={item.names.includes(router.route.name) ? 'page' : undefined}
            >
              <Icon name={item.icon} size={16} />
              {item.label}
            </a>
          </li>
        {/each}
      </ul>
    </nav>
    <div class="app-user">
      {#if session.user}
        <span class="app-user__name">{session.user.username}</span>
        <Badge tone={session.isAdmin ? 'skill' : 'neutral'}>{roleLabel(session.user.role)}</Badge>
      {/if}
      <button type="button" class="btn btn-quiet btn-sm" onclick={signOut}>
        <Icon name="logout" size={16} />
        Sign out
      </button>
    </div>
  </div>
</header>
