<script>
  import EmptyState from '../lib/components/EmptyState.svelte'
  import { router } from '../lib/router.svelte.js'
  import { hrefFor, paths } from '../lib/routes.js'
</script>

<EmptyState
  icon="search"
  heading="h1"
  title="Page not found"
  text="There is nothing at {router.route.path}. The link may be outdated or mistyped."
>
  <a class="btn btn-primary" href={hrefFor(paths.dashboard())}>Go to the dashboard</a>
  <a class="btn" href={hrefFor(paths.library())}>Browse the library</a>
</EmptyState>
